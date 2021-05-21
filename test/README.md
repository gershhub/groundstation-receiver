# Ground Station | Receiver Station

## Test Scripts

#### testUpload.py

`testUpload.py` performs a simple chunk upload test. It send pre-formatted audio and image files to S3 on a 60 second period, completing an entire satellite pass and simulating the behavior of the ground station system from the perspective of the app server. 

Its only external dependency is boto3, installable via pip. Boto3 must be configured with keys in advance. A configuration guide is available [here](https://boto3.amazonaws.com/v1/documentation/api/latest/guide/quickstart.html#configuration).
