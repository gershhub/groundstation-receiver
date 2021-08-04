import predict
from datetime import datetime, timezone
import pytz
import requests

# predictFuture.py predicts the passes of NOAA 15, NOAA 18, and NOAA 19 between start_time and end_time.
# Only passes with max elevation > minElev are shown, and pass info will print in local time, according to localtz.
# Note that the sign of qth longitude is reversed from normal for the predict library (west longitude).

# Set these parameters inline before running.
# to do: proper command line arguments
localtz = pytz.timezone('Asia/Hong_Kong')
start_time = datetime(
    2021, 
    8, 
    23, 
    0, 0, 0,
    tzinfo=localtz)
end_time = datetime(
    start_time.year, 
    start_time.month, 
    start_time.day + 1, 
    0, 0, 0, 
    tzinfo=localtz)
qth = (22.3010, -114.1590, 0)
minElev = 20

# will load updated TLE data in here
TLE = {
    'NOAA 15' : None,
    'NOAA 18' : None,
    'NOAA 19' : None,
}

# download the latest TLE file from the URL and save it to file path below
tleUrl = 'https://www.celestrak.com/NORAD/elements/noaa.txt'
tleFilePath = '/home/gershon/groundstation-data/TLE/noaa.txt'
try:
    response = requests.get(tleUrl)
    if(response.status_code==200 and response.content.decode().startswith('NOAA')):
        with open(tleFilePath, "wb") as f:
            f.write(response.content)
    else:
        print('Bad Response: Failed to update TLE')
except requests.ConnectionError:
    print('Connection Error: Failed to update TLE')
    raise

# load in and parse the latest TLE file
try:
    tleFile=open(tleFilePath)
    tleData=tleFile.readlines()
    tleFile.close()
    for satID in TLE.keys():
        tle=[]
        for (i, line) in enumerate(tleData):
            if(satID in line): 
                for l in tleData[i:i+3]:
                    tle.append(l.strip('\r\n').rstrip())
        TLE[satID] = tle
except OSError as e:
    print('OS Error: ' + e.strerror)
    raise

# for each NOAA satellite, run through predictions until we get to start_time.
# then print info for each qualifying pass until we pass end_time
for satID in TLE.keys():
    p = predict.transits(TLE[satID], qth)
    transit = next(p)
    while datetime.fromtimestamp(transit.start, tz=timezone.utc) < start_time:
        transit = next(p)

    while datetime.fromtimestamp(transit.start, tz=timezone.utc) < end_time:
        if(transit.peak()['elevation'] > minElev):
            datestring = str(datetime.fromtimestamp(transit.start, tz=localtz)).split('.')[0]
            print('{}: {} {}, duration={}s, max_elev={}'.format(satID, datestring, str(localtz).split('/')[1].replace('_', ' '), round(transit.duration()), round(transit.peak()['elevation'])))
        transit = next(p)
