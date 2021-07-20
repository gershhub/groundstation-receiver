import os, sys, subprocess, threading, time, math
import operator, json, logging
from datetime import datetime, timezone, timedelta
from uuid import uuid4
import sox, predict, boto3, cfg, requests


# overrides predict and forces the next satellite pass 2 seconds from script execution
testMode_recording = False

logging.basicConfig(format='%(levelname)s:%(message)s', level=logging.INFO)

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
    
    def predictNextPass(self, qth, minElev):
        p = predict.transits(self.TLE, qth)
        transit = next(p)
        while(transit.peak()['elevation'] < minElev):
            transit = next(p)
        dt_ts = datetime.fromtimestamp(transit.start, tz=timezone.utc)
        self.nextPass = SatPass(dt_ts,  transit.duration(), transit.peak()['elevation'])
        if testMode_recording:
            self.nextPass = SatPass(datetime.now(timezone.utc) + timedelta(seconds=2),  transit.duration(), transit.peak()['elevation'])
        return self.nextPass

class SatPass:
    def __init__(self, passTime, passDuration, passElevation):
        self.passTime = passTime
        self.duration = passDuration
        self.elevation = passElevation
        self.lastUpdated = datetime.now(timezone.utc)
        self.performanceId = None

class AWS:
    def __init__(self, region_name):
        self.s3 = boto3.resource('s3')
        self.sqsclient = boto3.client('sqs', region_name)
        self.sqs_passdata_url = None
        self.sqs_preview_url = None

        
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
    
    # the timing of the pass is not going to be very precise because of the apparent time required to release
    # the radio device between recordings (2 second sleep), but that should be ok
    timeLeft = duration
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
        if(filecount == 1): inform = True
        else: inform = False
        transcodeDecodeUploadThread = threading.Thread(
            target = transcodeDecodeUpload, 
            args = ( outfileName, filecount, passInfo, aws, inform)
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
            logging.info('Completed rtl_fm recording [chunk {}]'.format(filecount))
            logging.info('Starting decode thread [chunk {}]'.format(filecount))
            timeLeft = timeLeft - chunkDuration
            transcodeDecodeUploadThread.start()
            time.sleep(2) # 2 seconds for radio reset
        except OSError as e:
            logging.warning('OS Error during command: ' + ' '.join(cmdline))
            logging.warning('OS Error: ' + e.strerror)


# transcode raw recording file, process APT decode, upload to S3, remove files
# intended to be spun off as a thread while recording continues
def transcodeDecodeUpload(filename, filecount, passInfo, aws, inform=False):
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
    bucket_name = config.get('AWS', 's3_bucket')
    logging.info('Starting S3 upload sequence [chunk {}]'.format(filecount))
    img = open(out_img, 'rb')
    aws.s3.Bucket(bucket_name).put_object(Key='image/{}.png'.format(filename), Body=img)
    logging.info('Image upload completed [chunk {}]'.format(filecount))
    mp3 = open(out_mp3, 'rb')
    aws.s3.Bucket(bucket_name).put_object(Key='audio/{}.mp3'.format(filename), Body=mp3)
    logging.info('Audio upload completed [chunk {}]'.format(filecount))

    # on second chunk upload completed, inform the app server to begin performance
    if(inform):
        informSQSPass(aws, passInfo['satellite'], passInfo['minChunkDuration'], passInfo['maxChunkDuration'])


def informSQSPass(aws, satellite, minChunkDuration, maxChunkDuration):
    # include 2 second radio reset delay
    maxChunkDuration = maxChunkDuration + 2
    
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
    
    # predicted pass start time
    startTimestamp = math.ceil(satellite.nextPass.passTime.timestamp())
    
    segments = []
    for i in range(num_chunks):
        segments.append({
            'soundFile' : {
                'bucketName': 'ground-station-prod',
                'objectPath': 'audio/signalchunk_{}.mp3'.format(i)
            },
            'imageFile' : {
                'bucketName': 'ground-station-prod',
                'objectPath': 'image/signalchunk_{}.png'.format(i)
            }
        })
    
    message = {
        "performanceId": performanceId,
        "startTimestamp": startTimestamp,
        "duration": duration,
        "segments": segments
    }

    response = aws.sqsclient.send_message(
        QueueUrl=aws.sqs_passdata_url,
        MessageBody=json.dumps({'default': json.dumps(message)}),
        MessageGroupId='groundstation-receiver',
        MessageDeduplicationId=performanceId
    )
    logging.info('Sending SQS pass info: {}\n  --> SQS Response: {}'.format(str(message), response))
    return(response)

def informSQSPreview(aws, satellite):
    # send SQS message with upcoming pass data (preview)
    # satellite nextPass should already been assigned its unique performanceID before this is called
    message = {
        "nextsatelliteName": satellite.identifier,
        "nextperformanceStartTime": round(satellite.nextPass.passTime.timestamp()),
        "nextperformanceId": satellite.nextPass.performanceID,
    }
    response = aws.sqsclient.send_message(
        QueueUrl=aws.sqs_preview_url,
        MessageBody=json.dumps({'default': json.dumps(message)}),
        MessageGroupId='groundstation-receiver',
        MessageDeduplicationId=satellite.nextPass.performanceID
    )
    logging.info('Sending SQS preview info: {}\n  --> SQS Response: {}'.format(str(message), response))


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

    # global AWS object to be passed around
    region_name = config.get('AWS','region_name')
    aws = AWS(region_name)
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
    minChunkDuration = 20
    maxChunkDuration = 60

    # loop, sleeping until it's time to capture data
    while(True):
        # sort satellites by next pass, computed redundantly but not demanding for 3 satellites
        satQueue = sorted(satellites, key=lambda p : p.predictNextPass(qth, minElev).passTime)

        currentTime = datetime.now(timezone.utc)
        timeUntilPass = satQueue[0].nextPass.passTime - currentTime
        satQueue[0].nextPass.performanceID = str(uuid4()) # give the upcoming pass a unique ID

        # send SQS message with upcoming pass data
        informSQSPreview(aws, satQueue[0])

        if(timeUntilPass.total_seconds()>0):
            for sat in satQueue:
                logging.info(' {} at {} UTC, max elev. {} degrees'.format(sat.identifier, str(sat.nextPass.passTime).split('.')[0], round(sat.nextPass.elevation)))
            time.sleep(timeUntilPass.total_seconds())
        
        # just in case rtl_fm is still running, if python was shut down uncleanly
        tryKill('rtl_fm')

        logging.info('Beginning capture of {} at {}: duration {}, max_elev. {} degrees'.format(
            satQueue[0].identifier, 
            currentTime, 
            satQueue[0].nextPass.duration, 
            satQueue[0].nextPass.elevation 
        ))
        recordChunksFM(satQueue[0], minChunkDuration, maxChunkDuration, aws)
        
        # just in case rtl_fm is still running, if python was shut down uncleanly
        tryKill('rtl_fm')

        # pull TLEs from file once per day
        if (tleLastUpdated != datetime.now(timezone.utc).day):
            updateTLE(satellites, tlePath)
            tleLastUpdated = datetime.now(timezone.utc).day

        # sleep for a couple minutes
        time.sleep(90)