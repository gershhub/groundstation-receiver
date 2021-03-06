import os, sys, subprocess, threading, time, math
import operator, json, logging
from datetime import datetime, timezone, timedelta
from uuid import uuid4
import sox, predict, boto3, cfg, requests


# overrides predict and forces the next satellite pass 2 seconds from script execution
testMode_recording = False

# send recordings and metadata to AWS
upload = True

logging.basicConfig(format='%(levelname)s: %(message)s', level=logging.INFO)

# groundstation configuration 
configFile = 'groundstation.cfg'

# if no config file is given, use the one above
if len(sys.argv) > 2:
    logging.warning("Usage: {} [groundstation.cfg]".format(sys.argv[0]))
    exit(-1)
elif len(sys.argv) == 2:
    configFile = sys.argv[1]

# load config file
config = cfg.get(configFile)


# a handy place to keep state about the satellites being recording
class WeatherSatellite:
    def __init__(self, satID, frequency):
        self.identifier = satID
        self.frequency = frequency
        self.TLE = None
        self.nextPass = None
    
    def predictNextPass(self, qth, minElev, cut_start, cut_end):
        p = predict.transits(self.TLE, qth)
        transit = next(p)
        current_time = datetime.now(timezone.utc)
        while((transit.peak()['elevation'] < minElev) or transit.start-datetime.timestamp(current_time)<0 ):
            transit = next(p)
        dt_ts = datetime.fromtimestamp(transit.start + cut_start, tz=timezone.utc)
        self.nextPass = SatPass(dt_ts,  transit.duration()-(cut_start + cut_end), transit.peak()['elevation'])
        if testMode_recording:
            # transit.duration()
            self.nextPass = SatPass(datetime.now(timezone.utc) + timedelta(seconds=2), 120, transit.peak()['elevation'])
        return self.nextPass

class SatPass:
    def __init__(self, passTime, passDuration, passElevation):
        self.passTime = passTime
        self.duration = passDuration
        self.elevation = passElevation
        self.lastUpdated = datetime.now(timezone.utc)
        self.performanceId = None

class AWS:
    def __init__(self, s3_region, sqs_region):
        self.s3 = boto3.resource('s3', region_name=s3_region)
        self.s3_archive = boto3.resource('s3', region_name=sqs_region) # trying this in AP-EAST-1 (sqs region)
        self.sqsclient = boto3.client('sqs', region_name=sqs_region)
        self.sqs_passdata_url = None
        self.sqs_preview_url = None

# remove files (but not directories) from a given directory
def removeFiles(directory):
    for filename in os.listdir(directory):
        file_path = os.path.join(directory, filename)
        try:
            if os.path.isfile(file_path) or os.path.islink(file_path):
                os.unlink(file_path)
        except Exception as e:
            logging.warning('Error {}: failed to remove {}'.format(e, file_path))

# update an array of weather sats from a TLE file
def updateTLE(satellites, tleFilePath, tle_url):
    try:    
        response = requests.get(tle_url)
        if(response.status_code==200 and response.content.decode().startswith('NOAA')):
            with open(tleFilePath, "wb") as f:
                f.write(response.content)
                logging.info('Cached new TLE')
        else:
            logging.error('Bad Response: Failed to update TLE')
    except requests.ConnectionError:
        logging.error('Connection Error: Failed to update TLE')
        raise
    try:
        logging.info('Ingesting TLEs from {}'.format(tleFilePath))
        tleFile=open(tleFilePath)
        tleData=tleFile.readlines()
        tleFile.close()
        for sat in satellites:
            tle=[]
            for i, line in enumerate(tleData):
                if(sat.identifier in line): 
                    for l in tleData[i:i+3]:
                        tle.append(l.strip('\r\n').rstrip())
            sat.TLE = tle
    except OSError as e:
        logging.error('OS Error: ' + e.strerror)
        raise



# record demodulated signals over a given duration, breaking the recordings into chunks 
def recordChunksFM(satellite, minChunkDuration, maxChunkDuration, aws):

    # options for rtl_fm, which captures and demodulates FM signals
    # rtl_fm is an external application included with rtl-sdr
    rtl_fm = ['/usr/bin/rtl_fm',
               '-f', str(satellite.frequency),          # center frequency
               '-s', config.get('SDR', 'samplerate'),   # sample rate of demodulated signal
               '-g', config.get('SDR', 'gain'),         # SDR RF gain
               '-F', '9',                               # enable downsample filter
               '-E', 'dc',
               '-E', 'deemp',                           # enable de-emphasis filter
               '-p', config.get('SDR', 'shift'),        # SDR ppm error
               '-T']                                    # enable bias tee

    duration = math.floor(satellite.nextPass.duration)

    # number of chunks in total duration of pass
    num_chunks = duration / maxChunkDuration

    # cut off the last bit if it is less than minChunkDuration
    if(duration % maxChunkDuration >= minChunkDuration):
        num_chunks = math.ceil(num_chunks)
        logging.info('Beginning pass consisting of {}x {}s chunks and 1x {}s chunk'.format(num_chunks-1, maxChunkDuration, duration % maxChunkDuration))
    else:
        num_chunks = math.floor(num_chunks)
        logging.info('Beginning pass consisting of {}x {}s chunks, skipping last {}s of pass (< minChunkDuration)'.format(num_chunks, maxChunkDuration, duration % maxChunkDuration))
    
    logging.info('Loop will call RTL_FM with arguments: {}'.format(rtl_fm))

    # the timing of the pass is not going to be very precise because of the apparent time required to release
    # the radio device between recordings (2 second sleep), but that should be ok
    timeLeft = duration
    outfiles = []
    for filecount in range(num_chunks):
        outfileName = 'signalchunk_{}'.format(filecount)
        dataDir = config.get('OUTPUTS', 'dataDir')
        outfilePath_raw = os.path.join(dataDir, os.path.join(config.get('OUTPUTS', 'raw'), "{}.raw".format(outfileName)))

        # resample, APT decode, trancode, and upload are handled after rtl_fm, in a separate thread
        # after second chunk upload, inform the app server to begin performance
        passInfo = {
                'satellite' : satellite,
                'minChunkDuration' : minChunkDuration,
                'maxChunkDuration' : maxChunkDuration
            }
        if(filecount == 1): 
            inform = True
        else: 
            inform = False
        if(filecount == num_chunks-1): 
            allChunks = outfiles
        else: 
            allChunks = []
        transcodeDecodeUploadThread = threading.Thread(
            target = transcodeDecodeUpload, 
            args = ( outfileName, filecount, passInfo, aws, inform, allChunks)
        )

        try:
            logging.info('Starting rtl_fm recording [chunk {}]'.format(filecount))
            child = subprocess.Popen(rtl_fm + [outfilePath_raw])
            if(timeLeft >= maxChunkDuration):
                chunkDuration = maxChunkDuration
            else:
                chunkDuration = duration % maxChunkDuration
            time.sleep(chunkDuration)
            child.terminate()
            outfiles.append(outfileName)
            logging.info('Completed rtl_fm recording [chunk {}]'.format(filecount))
            logging.info('Starting decode thread [chunk {}]'.format(filecount))
            timeLeft = timeLeft - chunkDuration
            transcodeDecodeUploadThread.start()
            time.sleep(1) # 1 second for radio reset
        except OSError as e:
            logging.warning('OS Error during command: ' + ' '.join(cmdline))
            logging.warning('OS Error: ' + e.strerror)


# transcode raw recording file, process APT decode, upload to S3, remove files
# intended to be spun off as a thread while recording continues
def transcodeDecodeUpload(filename, filecount, passInfo, aws, inform=False, allChunks=[]):
    dataDir = config.get('OUTPUTS', 'dataDir')
    in_raw = os.path.join(dataDir, os.path.join(config.get('OUTPUTS', 'raw'), '{}.raw'.format(filename)))
    out_wav = os.path.join(dataDir, os.path.join(config.get('OUTPUTS', 'wav'), '{}.wav'.format(filename)))
    out_mp3 = os.path.join(dataDir, os.path.join(config.get('OUTPUTS', 'mp3'), '{}.mp3'.format(filename)))
    out_img = os.path.join(dataDir, os.path.join(config.get('OUTPUTS', 'img'), '{}.png'.format(filename)))

    # sox transformer: raw to wav
    sox_raw2wav = sox.Transformer()
    sox_raw2wav.set_input_format(file_type='raw',rate=int(config.get('SDR', 'samplerate')),bits=16,channels=1,encoding='signed-integer')
    sox_raw2wav.set_output_format(file_type='wav',rate=int(config.get('SDR', 'wavrate')))
    logging.info('Starting raw to wav with sox [chunk {}]'.format(filecount))
    success = sox_raw2wav.build(in_raw, out_wav)
    if not success:
        logging.warning('Raw to wav resample failed! [chunk {}]'.format(filecount))

    # sox transformer: raw to mp3
    sox_raw2mp3 = sox.Transformer()
    sox_raw2mp3.set_input_format(file_type='raw',rate=int(config.get('SDR', 'samplerate')),bits=16,channels=1,encoding='signed-integer')
    sox_raw2mp3.set_output_format(file_type='mp3',rate=int(config.get('SDR', 'mp3rate')))
    logging.info('Starting sox raw to mp3 with sox [chunk {}]'.format(filecount))
    success = sox_raw2mp3.build(in_raw, out_mp3)
    if not success:
        logging.warning('Raw to mp3 resample/transcode failed! [chunk {}]'.format(filecount))

    # decode APT from wav
    logging.info('Starting APT decode [chunk {}]'.format(filecount))
    # aptdec = ['aptdec', out_wav, '-o', os.path.relpath(out_img)]
    satid = passInfo['satellite'].identifier.lower().replace(' ', '_')
    tlePath = os.path.join(config.get('TLE', 'tleDir'), config.get('TLE', 'tleFile'))
    aptdec = ['noaa-apt', out_wav, '-o', os.path.relpath(out_img), '-T', tlePath, '-s', satid, '-c', 'histogram']
    
    proc = subprocess.Popen(aptdec)
    proc.wait()

    # upload files to S3
    if(upload):
        bucket_name = config.get('AWS', 's3_bucket')
        logging.info('Starting S3 upload sequence [chunk {}]'.format(filecount))
        img = open(out_img, 'rb')
        aws.s3.Bucket(bucket_name).put_object(Key='image/{}.png'.format(filename), Body=img)
        logging.info('Image upload completed [chunk {}]'.format(filecount))
        img.close()
        mp3 = open(out_mp3, 'rb')
        aws.s3.Bucket(bucket_name).put_object(Key='audio/{}.mp3'.format(filename), Body=mp3)
        logging.info('Audio upload completed [chunk {}]'.format(filecount))
        mp3.close()
    else:
        logging.info('Uploading skipped [chunk {}]'.format(filecount))

    # on second chunk upload completed, inform the app server to begin performance
    if(inform):
        informSQSPass(aws, passInfo['satellite'], passInfo['minChunkDuration'], passInfo['maxChunkDuration'])
    
    # on the last chunk, perform an archiving routine that combines all audio files and decodes a complete image
    # archives are uploaded to a separate s3 bucket for safekeeping
    if(allChunks):
        logging.info('Beginning pass archiving routine')
        archive_path = os.path.join(dataDir, config.get('OUTPUTS','archive'))

        # first, remove the last pass archive files
        removeFiles(archive_path)
        
        # archive filenames follow a timestamp_satID format 
        archive_filename = '{}_{}'.format(
            passInfo['satellite'].nextPass.passTime.strftime('%Y-%m-%d-%H-%M-%S-%Z'),
            passInfo['satellite'].identifier.replace(' ', '-'))
        archive_filepath_wav = os.path.join(archive_path, '{}.wav'.format(archive_filename))
        archive_filepath_mp3 = os.path.join(archive_path, '{}.mp3'.format(archive_filename))
        archive_filepath_image = os.path.join(archive_path, '{}.png'.format(archive_filename))

        # convert list of recorded chunks into paths to wav files
        allChunksPath = list(map(lambda fn: os.path.join(dataDir, os.path.join(config.get('OUTPUTS', 'wav'), '{}.wav'.format(fn))), allChunks))
        allChunksFormat = list(map(lambda fmt: 'wav', allChunks))
        rate = int(config.get('SDR','wavrate'))
        allChunksRate = list(map(lambda r: rate, allChunks))
        
        # combine all recording wav files into one file using sox
        soxcombiner = sox.combine.Combiner()
        soxcombiner.set_input_format(file_type=allChunksFormat, rate=allChunksRate)
        success = soxcombiner.build(input_filepath_list=allChunksPath, output_filepath=archive_filepath_wav, combine_type='concatenate')
        if not success:
            logging.warning('Wav file archive failed! [{}]'.format(archive_filename))

        # transcode to mp3
        sox_wav2mp3 = sox.Transformer()
        sox_wav2mp3.set_input_format(file_type='wav',rate=int(config.get('SDR', 'wavrate')))
        sox_wav2mp3.set_output_format(file_type='mp3',rate=int(config.get('SDR', 'mp3rate')))
        logging.info('Starting sox wav to mp3 [{}]'.format(archive_filename))
        # encode the combined wav file to mp3
        success = sox_wav2mp3.build(archive_filepath_wav, archive_filepath_mp3)
        if not success:
            logging.warning('Wav to mp3 resample/transcode failed! [{}]'.format(archive_filename))

        satid = passInfo['satellite'].identifier.lower().replace(' ', '_')
        logging.info('Starting APT decode for archive [{}]'.format(archive_filename))
        aptdec = ['noaa-apt', archive_filepath_wav, '-o', os.path.relpath(archive_filepath_image), '-T', tlePath, '-s', satid, '-c', 'histogram']
        proc = subprocess.Popen(aptdec)
        proc.wait()
        
        if(upload):
            logging.info('Starting S3 upload sequence for archive [{}]'.format(archive_filename))
            archive_bucket_name = config.get('AWS', 's3_bucket_archive')              
            img = open(archive_filepath_image, 'rb')
            aws.s3_archive.Bucket(archive_bucket_name).put_object(Key='images/{}.png'.format(archive_filename), Body=img)
            logging.info('Image upload completed [{}]'.format(archive_filename))
            img.close()

            mp3 = open(archive_filepath_mp3, 'rb')
            aws.s3_archive.Bucket(archive_bucket_name).put_object(Key='audio/{}.mp3'.format(archive_filename), Body=mp3)
            logging.info('Audio upload completed [{}]'.format(archive_filename))
            mp3.close()
        else:
            logging.info('Skipping S3 upload for archive [{}]'.format(archive_filename))
        logging.info('Completed pass archiving routine')

def informSQSPass(aws, satellite, minChunkDuration, maxChunkDuration):
    # include 1 second radio reset delay
    maxChunkDuration = maxChunkDuration + 1
    
    # zero padded number of recordings made since script start
    performanceId = satellite.nextPass.performanceID

    # number of chunks in total duration of pass
    num_chunks = satellite.nextPass.duration / maxChunkDuration

    # cut off the last bit if it is less than minChunkDuration 
    if(satellite.nextPass.duration % maxChunkDuration >= minChunkDuration):
        num_chunks = math.ceil(num_chunks)
        duration = math.floor(satellite.nextPass.duration)
    else:
        num_chunks = math.floor(num_chunks)
        duration = math.floor(satellite.nextPass.duration - satellite.nextPass.duration % maxChunkDuration)
    
    # predicted pass start time, including website time delay
    startTimestamp = math.ceil(satellite.nextPass.passTime.timestamp()) + 2*(maxChunkDuration+1)
    
    segments = []
    for i in range(num_chunks):
        segments.append({
            'soundFile' : {
                'bucketName': 'ground-station-prod-hk-2',
                'objectPath': 'audio/signalchunk_{}.mp3'.format(i)
            },
            'imageFile' : {
                'bucketName': 'ground-station-prod-hk-2',
                'objectPath': 'image/signalchunk_{}.png'.format(i)
            }
        })
    
    message = {
        "performanceId": performanceId,
        "startTimestamp": startTimestamp,
        "duration": duration,
        "segments": segments
    }

    if(upload):
        response = aws.sqsclient.send_message(
            QueueUrl=aws.sqs_passdata_url,
            MessageBody=json.dumps(message),
            MessageGroupId='groundstation-receiver',
            MessageDeduplicationId=performanceId
        )
        logging.info('Sending SQS pass info: {}\n  --> SQS Response: {}'.format(str(message), response))
    else:
        logging.info('Skipped sending SQS pass info: {}'.format(str(message)))

def informSQSPreview(aws, satellite, maxChunkDuration):
    # send SQS message with upcoming pass data (preview)
    # satellite nextPass should already been assigned its unique performanceID before this is called
    # website time delay included in start time (2x chunk duration)
    message = {
        "nextsatelliteName": satellite.identifier,
        "nextperformanceStartTime": round(satellite.nextPass.passTime.timestamp()) + 2*(maxChunkDuration+1), 
        "nextperformanceId": satellite.nextPass.performanceID,
    }
    if(upload):
        response = aws.sqsclient.send_message(
            QueueUrl=aws.sqs_preview_url,
            MessageBody=json.dumps(message),
            MessageGroupId='groundstation-receiver',
            MessageDeduplicationId=satellite.nextPass.performanceID
        )
        logging.info('Sending SQS preview info: {}\n  --> SQS Response: {}'.format(str(message), response))
    else:
        logging.info('Skipped sending SQS preview: {}'.format(str(message)))


# given a process name, if it is found running, kill it
def tryKill(processname):
    proc = subprocess.Popen(["pgrep", processname], stdout=subprocess.PIPE) 
    if(list(proc.stdout)):
        os.system('killall -9 {}'.format(processname))
        logging.info('Errant {} process discovered and killed!'.format(processname))


if __name__ == "__main__":
    # TLE file should be updated regularly
    tlePath = os.path.join(config.get('TLE', 'tleDir'), config.get('TLE', 'tleFile'))
    tleUrl = config.get('TLE', 'tleUrl') 
    
    # QTH (ground location)
    qth = (float(config.get('QTH','lat')), float(config.get('QTH','lon')), float(config.get('QTH','alt')))

    # minimum elevation (angle in degrees) considered for recording
    minElev = float(config.get('QTH', 'minElev'))

    # time (in seconds) to cut from the beginning and end of the full pass to avoid recording noise
    cut_start = float(config.get('OUTPUTS','cut_start'))
    cut_end = float(config.get('OUTPUTS','cut_end'))

    # global AWS object to be passed around
    s3_region = config.get('AWS','s3_region')
    sqs_region = config.get('AWS','sqs_region')
    aws = AWS(s3_region=s3_region, sqs_region=sqs_region)
    aws.sqs_passdata_url = config.get('AWS', 'sqs_passdata_url')
    aws.sqs_preview_url = config.get('AWS', 'sqs_preview_url')

    satIDs = config.getlist('SATELLITES', 'identifiers')
    frequencies = config.getlist('SATELLITES', 'frequencies')
    satellites = []
    for satID, frequency in zip(satIDs, frequencies):
        satellites.append(WeatherSatellite(satID, frequency))

    updateTLE(satellites, tlePath, tleUrl)
    tleLastUpdated = datetime.now(timezone.utc).day

    # min and max chunk durations for recordings
    minChunkDuration = int(config.get('SDR', 'minChunkDuration'))
    maxChunkDuration = int(config.get('SDR', 'maxChunkDuration'))

    # loop, sleeping until it's time to capture data
    while(True):
        # sort satellites by next pass, computed redundantly but not demanding for 3 satellites
        satQueue = sorted(satellites, key=lambda p : p.predictNextPass(qth, minElev, cut_start, cut_end).passTime)
        nextSat = satQueue[0]

        # send SQS message with upcoming pass data
        nextSat.nextPass.performanceID = str(uuid4()) # give the upcoming pass a unique ID
        informSQSPreview(aws, nextSat, maxChunkDuration)

        timeUntilPass = nextSat.nextPass.passTime - datetime.now(timezone.utc)
        if(timeUntilPass.total_seconds()>0):
            for sat in satQueue:
                logging.info(' {} at {} UTC, max elev. {} degrees'.format(sat.identifier, str(sat.nextPass.passTime).split('.')[0], round(sat.nextPass.elevation)))
            time.sleep(timeUntilPass.total_seconds())
        
        # just in case rtl_fm is still running, if python was shut down uncleanly
        tryKill('rtl_fm')

        logging.info('Beginning capture of {} at {} {}: duration {}, max_elev. {} degrees'.format(
            nextSat.identifier, 
            str(datetime.now(timezone.utc)).split('.')[0], 
            str(timezone.utc),
            round(nextSat.nextPass.duration), 
            round(nextSat.nextPass.elevation)
        ))
        recordChunksFM(nextSat, minChunkDuration, maxChunkDuration, aws)
        
        # just in case rtl_fm is still running, if python was shut down uncleanly
        tryKill('rtl_fm')

        # pull TLEs from file once per day
        if (tleLastUpdated != datetime.now(timezone.utc).day):
            updateTLE(satellites, tlePath)
            tleLastUpdated = datetime.now(timezone.utc).day

        # sleep for a couple minutes
        time.sleep(90)