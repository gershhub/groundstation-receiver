import os, sys, subprocess, threading, time, math
import operator, json, logging
from datetime import datetime, timezone, timedelta
from uuid import uuid4
import sox, predict, boto3, requests
from random import randint

sys.path.append('../')
import cfg
from groundstation import WeatherSatellite, SatPass, AWS, informSQSPass, informSQSPreview

logging.basicConfig(format='%(levelname)s: %(message)s', level=logging.INFO)

# groundstation configuration 
configFile = '../groundstation.cfg'
# if no config file is given, use the one above
if len(sys.argv) > 2:
    logging.warning("Usage: {} [groundstation.cfg]".format(sys.argv[0]))
    exit(-1)
elif len(sys.argv) == 2:
    configFile = sys.argv[1]

# load config file
config = cfg.get(configFile)

if __name__ == "__main__":

    # global AWS object passed around
    # configure endpoints, region, etc in groundstation.cfg
    region_name = config.get('AWS','region_name')
    aws = AWS(region_name)
    aws.sqs_passdata_url = config.get('AWS', 'sqs_passdata_url')
    aws.sqs_preview_url = config.get('AWS', 'sqs_preview_url')

    # fakePass uses data from a 2021-07-10 10:22:27 UTC NOAA 19 pass over Hong Kong
    # data will be presented as new (current)
    satQueue = [WeatherSatellite('NOAA 19', 137100000)]

    # pass start will occur after 90 seconds + random short delay from pass preview message
    randomDelay = randint(1,5)

    # pass loop will begin in 90ish seconds to account for preview message + randomDelay
    # duration includes a 2s gap between uploaded recordings: 14*(60s + 2s) + 54s = 922s
    # maximum elevation of this pass was 36 degrees
    satQueue[0].nextPass = SatPass(datetime.now(timezone.utc) + timedelta(seconds=(90 + randomDelay)),  922, 36)
    satQueue[0].nextPass.performanceID = str(uuid4()) # give the fake pass a unique ID

    # send SQS message with upcoming pass data
    informSQSPreview(aws, satQueue[0])

    # wait for 90 seconds (normal delay)
    logging.info('Sleeping for {} seconds until next pass'.format(str(90 + randomDelay)))
    time.sleep(90)

    # wait for another few, randomly chosen seconds
    time.sleep(randomDelay) 

    # sort and iterate through image and audio chunks
    filecount = 0
    for image_filename in sorted(os.listdir('test_data/img/')):
        # sleep for a minute to simulate recording time
        logging.info('Fake recording in progress...sleeping for 60 seconds')
        time.sleep(60)

        # get and format file paths
        filename = os.path.splitext(image_filename)[0]
        out_img = os.path.join('test_data/img', image_filename)
        audio_filename = '{}.mp3'.format(filename)
        out_mp3 = os.path.join('test_data/audio', audio_filename)

        # upload files to S3
        bucket_name = config.get('AWS', 's3_bucket')
        logging.info('Starting S3 upload sequence [chunk {}]'.format(filecount))
        
        img = open(out_img, 'rb')
        aws.s3.Bucket(bucket_name).put_object(Key='image/{}.png'.format(filename), Body=img)
        logging.info('Image upload completed [chunk {}]'.format(filecount))

        mp3 = open(out_mp3, 'rb')
        aws.s3.Bucket(bucket_name).put_object(Key='audio/{}.mp3'.format(filename), Body=mp3)
        logging.info('Audio upload completed [chunk {}]'.format(filecount))

        # issue SQS performance message once per performance, after second S3 upload
        if(filecount==1):
            minChunkDuration = 20
            maxChunkDuration = 60
            informSQSPass(aws, satQueue[0], minChunkDuration, maxChunkDuration)
        
        # increment upload counter
        filecount = filecount + 1

        # 2 second delay between recording segments allows for radio to reset
        logging.info('Fake radio reset delay...sleeping for 2 seconds')
        time.sleep(2)