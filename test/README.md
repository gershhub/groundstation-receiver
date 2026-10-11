# Ground Station | Receiver Station

## Test Scripts

### TLE regression tests

Run `python3 -m unittest discover -s test -p 'test_tle.py' -v` from the repository root after installing `requests`. These tests need no radio hardware, prediction libraries or AWS credentials. They check successful downloads, exact satellite matching, corrupt/partial data, HTTP errors and redirects, connection/read timeouts, atomic cache failures, the UTC daily refresh and both utility integrations. Shell integration tests use a local HTTP fixture and temporary directories, including paths with spaces. They make no external requests and do not run `groundstation.py` or `fakePass.py`.

The fixture `fixtures/noaa.txt` contains the three NOAA records retrieved once from CelesTrak's supported name query on 2026-10-11. It is fixed test data, not a runtime cache or a source of historical pass predictions.

### fakePass

`fakePass.py` uses pre-recorded audio and image chunks to simulate a NOAA satellite flight pass in order to test our AWS-hosted web application server. The application server is not included in this repository. 

#### Usage

Aftering configuring groundstation.cfg as well as local user AWS login credentials, run `python3 fakePass.py ../groundstation.cfg`.

#### Description

fakePass uses data from a 2021-07-10 10:22:27 UTC NOAA 19 pass over Hong Kong. Metadata are faked to simulate an imminent flight pass and recordings are presented as current. At start, fakePass sends a pass preview message to the SQS queue groundStationPreviewMessageQueueProd.fifo URL given in groundstation.cfg. The predicted pass time is given as between 91 and 94 seconds from script start (some small randomization for fun), and the duration is given as 922 seconds, corresponding to the recorded data. The script then sleeps for that ~91s period plus an additional 60s to simulate the radio chunk recording time. 

fakePass then sends the audio and image files to S3 in sequence, sleeping for 60 + 2 seconds between each upload. Immediately after the second upload, a second SQS message is issued, this time to groundStationPerformanceMessageQueueProd.fifo. This SQS message describes the files the application server should expect over the remainder of the pass. 

Schema for both SQS messages are given in the main project README.

#### Notes

Boto3 must be configured with access keys in advance. A configuration guide is available [here](https://boto3.amazonaws.com/v1/documentation/api/latest/guide/quickstart.html#configuration).
