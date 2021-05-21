# Ground Station | Receiver Station

## Test Scripts

#### testUpload.py

`testUpload.py` performs a simple chunk upload test. It send pre-formatted audio (mp3) and image (png) files to S3 on a 60s upload period, simulating the action of the ground station system for testing the app server and client side. Its only external dependency is boto3, installable via pip. Boto3 must be configured with keys in advance.