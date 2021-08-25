import predict
from datetime import datetime, timezone
import pytz
import requests
import argparse

# predictFuture.py predicts the passes of NOAA 15, NOAA 18, and NOAA 19 between start_time and end_time (start_time + 1 day).
# Only passes with max elevation > minElev are shown, and pass info will print in local time, according to localtz.
#
# Internet access is required in order to retrieve TLE telemetry data from Celestrak.
# TLE file noaa.txt is written to /tmp by default.
#
# Note that the sign of qth longitude is reversed internally from normal (e.g. Google) for the predict library (west longitude).
# The sign of longitude should be provided according to Google's convention (right click on Google Maps to copy your location).
#
# example usage (altitude and elevation are optional):
# python predictFuture.py --timezone America/New_York --date 2021-09-01 --gps 40.74864 -73.9863 --altitude 0 --elevation 20

def checkTimezoneFormat(s):
    try:
        return pytz.timezone(s)
    except pytz.UnknownTimeZoneError:
        raise argparse.ArgumentTypeError('Timezone must be given in standard form: see --help or pytz.common_timezones.')

def checkDateFormat(s):
    try:
        return datetime.strptime(s, "%Y-%m-%d")
    except ValueError:
        raise argparse.ArgumentTypeError('Date format should be YYYY-MM-DD.')

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "-z", 
        "--timezone",  
        help="Local timezone: " + str(pytz.common_timezones), 
        type=str, 
        required=True)
    parser.add_argument(
        "-d", 
        "--date", 
        help="Date to predict as YYYY-MM-DD", 
        required=True, 
        type=checkDateFormat)
    parser.add_argument(
        "-g", 
        "--gps", 
        type=float, 
        help="GPS coordinates in decimal degrees", 
        required=True, 
        nargs=2)
    parser.add_argument(
        "-a", 
        "--altitude", 
        type=int, 
        help="Ground station altitude in meters (default=0)", 
        default=0)
    parser.add_argument(
        "-e", 
        "--elevation", 
        type=int, 
        help="Minimum qualifying maximum elevation in degrees (default=20)", 
        default=20)
    args = parser.parse_args()



    localtz = pytz.timezone(args.timezone)
    # start_time = datetime.strptime(args.date, '%Y-%m-%d')
    start_time = localtz.localize(args.date)


    # by default, just predicting all passes in one day
    end_time = datetime(
        start_time.year, 
        start_time.month, 
        start_time.day + 1, 
        0, 0, 0, 
        tzinfo=localtz)

    # qth = (48.40745083192718, -2.69606179294, 0)
    qth = args.gps
    qth[1] = -qth[1]
    qth.append(args.altitude)
    
    minElev = args.elevation

    # will load updated TLE data in here
    TLE = {
        'NOAA 15' : None,
        'NOAA 18' : None,
        'NOAA 19' : None,
    }

    # download the latest TLE file from the URL and save it to file path below
    tleUrl = 'https://www.celestrak.com/NORAD/elements/noaa.txt'
    tleFilePath = '/tmp/noaa.txt'
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