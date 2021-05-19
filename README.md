# Ground Station | Receiver Station

This project contains notes, code, and setup instructions for the Ground Station NOAA satellite receiver, which autonomously collects APT transmissions from weather satellites NOAA-15, NOAA-18, and NOAA-19 as they fly overhead, breaks the transmissions into short, CDN-able chunks, and pushes the chunks to a cloud webserver.

The code is intended to be deployed on a Raspberry Pi 4 running Arch Linux ARM and Python 3, and depends on several other packages.

The Receiver Station is part of a commission by the M+ museum in Hong Kong.

#### Setup notes

### Dependencies

- [pypredict](https://github.com/nsat/pypredict): build from source on arch to avoid a urllib2 / python3 issue
- [rtl-sdr](https://github.com/osmocom/rtl-sdr): install bleeding edge via rtl-sdr-git in the AUR
- supervisord

