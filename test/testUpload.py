import time
import os
import boto3

s3_bucket = 'ground-station-prototype-eb'

if __name__ == "__main__":
    # AWS S3 object
    s3 = boto3.resource('s3')

    # sort and iterate through image and audio chunks
    for image_file in sorted(os.listdir('test_data/image/')):
        img_path = os.path.join('test_data/image', image_file)
        audio_file = '{}.mp3'.format(os.path.splitext(image_file)[0])
        audio_path = os.path.join('test_data/audio', audio_file)

        print('Uploading {} and {} to S3 bucket {}'.format(image_file, audio_file, s3_bucket))
        
        img = open(img_path, 'rb')
        try:
            s3.Bucket('ground-station-prototype-eb').put_object(Key='image/{}'.format(image_file), Body=img)
        except ClientError as e:
            print(e)
        img.close()

        audio = open(audio_path, 'rb')
        try:
            s3.Bucket('ground-station-prototype-eb').put_object(Key='audio/{}'.format(audio_file), Body=audio)
        except ClientError as e:
            print(e)
        audio.close()
        
        print('Sleeping for 1 minute')
        time.sleep(60)