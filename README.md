# Ground Station | Receiver Station

This project contains notes, code, and setup instructions for a Ground Station NOAA satellite receiver, which autonomously collects APT transmissions from weather satellites NOAA-15, NOAA-18, and NOAA-19 as they fly overhead, breaks the transmissions into short, CDN-able chunks, and pushes the chunks to a cloud webserver.

The code is intended to be deployed on a Raspberry Pi 4 running Arch Linux ARM and Python 3, and depends on several other packages.

Receiver Station draws on portions of [autowx](https://github.com/cyber-atomus/autowx), a similar project, and several others. On the recording side, the most significant functional difference between Receiver Station and autowx is the breaking up of the audio recording and decoded image into short chunks for a close-to-realtime online experience. Receiver Station also depends on Xerbo's aptdec instead of wxtoimg.

#### Setup notes



### Dependencies

- [predict](https://github.com/kd2bd/predict/): install from repo
- [pypredict](https://github.com/nsat/pypredict): build from source to avoid a urllib2 / python3 issue
- boto3: pip install boto3
- sox: install from repo
- twolame: install from repo
- pysox: pip install pysox
- configparser: pip install configparser
- [aptdec](https://github.com/Xerbo/aptdec)

