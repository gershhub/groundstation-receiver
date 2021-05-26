import os, sys, subprocess, threading, operator, time, logging
from datetime import datetime, timezone, timedelta
import sox
import predict
import boto3
import cfg

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

# TLE file should be updated on a regular basis by a separate process
tlePath = os.path.join(config.get('DIRS', 'tleDir'), config.get('DIRS', 'tleFile'))

# QTH (ground location)
qth = (float(config.get('QTH','lat')), float(config.get('QTH','lon')), float(config.get('QTH','alt')))

# minimum elevation (angle in degrees) considered for recording
minElev = float(config.get('QTH', 'minElev'))

# global AWS S3 object
s3 = boto3.resource('s3')

# a handy place to keep state about the satellites being recording
class WeatherSatellite:
    def __init__(self, satID, frequency):
        self.identifier = satID
        self.frequency = frequency
        self.TLE = None
        self.nextPass = None
    
    def predictNextPass(self, qth):
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
        
# update an array of weather sats from a TLE file
def updateTLE(satellites, tleFilePath):
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
        logging.warning('OS Error during command: ' + ' '.join(cmdline))
        logging.warning('OS Error: ' + e.strerror)



# record demodulated signals over a given duration, breaking the recordings into chunks 
def recordChunksFM(frequency, totalDuration, chunkDuration):
    # options for rtl_fm, which captures and demodulates FM signals
    # rtl_fm is an external application included with rtl-sdr
    rtl_fm = ['/usr/bin/rtl_fm',
               '-f', str(frequency),                    # center frequency
               '-s', config.get('SDR', 'samplerate'),   # sample rate of demodulated signal
               '-g', config.get('SDR', 'gain'),         # SDR RF gain
               '-F', '9',                               # enable downsample filter
               '-E', 'deemp',                           # enable de-emphasis filter
               '-p', config.get('SDR', 'shift')]        # SDR ppm error
            #    '-T']                                    # enable bias tee
    timeLeft = totalDuration
    filecount = 0
    while(timeLeft > 20):
        outfileName = 'signalchunk_{}'.format(filecount)
        outfilePath_raw = os.path.join(config.get('DIRS', 'raw'), "{}.raw".format(outfileName))
        transcodeDecodeUploadThread = threading.Thread(target=transcodeDecodeUpload, args=(outfileName,))
        try:
            logging.info('Starting RF recording and demod (rtl_fm): chunk {}'.format(filecount))
            child = subprocess.Popen(rtl_fm + [outfilePath_raw])
            time.sleep(chunkDuration)
            child.terminate()
            filecount = filecount + 1
            timeLeft = timeLeft - chunkDuration
            logging.info('Starting transcode/decode/upload thread')
            transcodeDecodeUploadThread.start()
            time.sleep(2)
        except OSError as e:
            logging.warning('OS Error during command: ' + ' '.join(cmdline))
            logging.warning('OS Error: ' + e.strerror)


# transcode raw recording file, process APT decode, upload to S3, remove files
# intended to be spun off as a thread while recording continues
def transcodeDecodeUpload(filename):
    dataDir = config.get('DIRS', 'dataDir')
    in_raw = os.path.join(dataDir, os.path.join(config.get('DIRS', 'raw'), '{}.raw'.format(filename)))
    out_wav = os.path.join(dataDir, os.path.join(config.get('DIRS', 'wav'), '{}.wav'.format(filename)))
    out_mp3 = os.path.join(dataDir, os.path.join(config.get('DIRS', 'mp3'), '{}.mp3'.format(filename)))
    out_img = os.path.join(dataDir, os.path.join(config.get('DIRS', 'img'), '{}.png'.format(filename)))

    # sox transformer: raw to wav
    sox_raw2wav = sox.Transformer()
    sox_raw2wav.set_input_format(file_type='raw',rate=int(config.get('SDR', 'samplerate')),bits=16,channels=1,encoding='signed-integer')
    sox_raw2wav.set_output_format(file_type='wav',rate=int(config.get('SDR', 'wavrate')))
    logging.info('Starting raw to wav resample (sox)')
    success = sox_raw2wav.build(in_raw, out_wav)
    if not success:
        logging.warning("Raw to wav resample failed!")

    # sox transformer: raw to mp3
    sox_raw2mp3 = sox.Transformer()
    sox_raw2mp3.set_input_format(file_type='wav',rate=int(config.get('SDR', 'samplerate')),bits=16,channels=1,encoding='signed-integer')
    sox_raw2mp3.set_output_format(file_type='mp3',rate=int(config.get('SDR', 'mp3rate')))
    logging.info('Starting raw to mp3 resample/transcode (sox)')
    success = sox_raw2wav.build(in_raw, out_mp3)
    if not success:
        logging.warning("Raw to mp3 resample/transcode failed!")

    # aptdec: decode APT from wav
    logging.info('Starting APT decode (aptdec)')
    aptdec = ['aptdec', out_wav, '-o', os.path.relpath(out_img)]
    proc = subprocess.Popen(aptdec)
    proc.wait()

    # upload files to S3
    logging.info('Starting image chunk upload')
    img = open(out_img, 'rb')
    s3.Bucket('ground-station-prototype-eb').put_object(Key='image/{}.png'.format(filename), Body=img)
    logging.info('Starting audio chunk upload')
    mp3 = open(out_mp3, 'rb')
    s3.Bucket('ground-station-prototype-eb').put_object(Key='audio/{}.mp3'.format(filename), Body=mp3)

    # remove files from local 
    # logging.info('Removing local files')
    # os.remove(in_raw)
    # os.remove(out_wav)
    # os.remove(out_mp3)
    # os.remove(out_img)
    

if __name__ == "__main__":
    satIDs = config.getlist('SATELLITES', 'identifiers')
    frequencies = config.getlist('SATELLITES', 'frequencies')
    satellites = []
    for satID, frequency in zip(satIDs, frequencies):
        satellites.append(WeatherSatellite(satID, frequency))
    
    updateTLE(satellites, tlePath)
    tleLastUpdated = datetime.now(timezone.utc).day

    # loop until it's time to capture data
    while(True):
        satQueue = sorted(satellites, key=lambda p : p.predictNextPass(qth).passTime )
        nextSat = satQueue[0]
        currentTime = datetime.now(timezone.utc)
        timeUntilPass = satQueue[0].nextPass.passTime - currentTime

        if(timeUntilPass.total_seconds()>0):
            logging.info('Waiting for {} in {} from now at {} UTC'.format(nextSat.identifier, timeUntilPass, nextSat.nextPass.passTime))
            logging.info('Next: Waiting for {} in {} from now at {} UTC'.format(satQueue[1].identifier, satQueue[1].nextPass.passTime - currentTime, satQueue[1].nextPass.passTime))
            logging.info('Next: Waiting for {} in {} from now at {} UTC'.format(satQueue[2].identifier, satQueue[2].nextPass.passTime - currentTime, satQueue[2].nextPass.passTime))
            time.sleep(timeUntilPass.total_seconds())
        
        logging.info('Beginning capture of {} at {}: duration {}, elevation {} degrees'.format(nextSat.identifier, currentTime, nextSat.nextPass.duration, nextSat. nextPass.elevation ))
        recordChunksFM(nextSat.frequency, nextSat.nextPass.duration, 90)
           
        # pull TLEs from file once per day
        if (tleLastUpdated != datetime.now(timezone.utc).day):
            updateTLE(satellites, tlePath)
            tleLastUpdated = datetime.now(timezone.utc).day

        time.sleep(1)