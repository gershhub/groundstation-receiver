# Ground Station | Receiver Station

## Test Scripts

#### fakePass

`fakePass.py` uses pre-recorded audio and image chunks to simulate a NOAA satellite flight pass in order to test our AWS-hosted web application server. The application server is not included in this repository. 

fakePass uses data from a 2021-07-10 10:22:27 UTC NOAA 19 pass over Hong Kong. Metadata are faked to simulate an imminent flight pass and recordings are presented as current. At start, fakePass sends a pass preview message to the SQS queue groundStationPreviewMessageQueueProd.fifo URL given in groundstation.cfg. The predicted pass time is given as between 91 and 94 seconds from script start (some small randomization for fun), and the duration is given as 922 seconds, corresponding to the recorded data. The script then sleeps for that ~91s period plus an additional 60s to simulate the radio chunk recording time. 

fakePass then sends the audio and image files to S3 in sequence, sleeping for 60 + 2 seconds between each upload. Immediately after the second upload, a second SQS message is issued, this time to groundStationPerformanceMessageQueueProd.fifo. This SQS message describes the files the application server should expect over the remainder of the pass. 

Schema for both SQS messages are given in the main project README.

#### Notes

Boto3 must be configured with access keys in advance. A configuration guide is available [here](https://boto3.amazonaws.com/v1/documentation/api/latest/guide/quickstart.html#configuration).
