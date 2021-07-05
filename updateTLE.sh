#!/bin/bash
TLEDIR=/home/slowimmediate/groundstation-data/TLE

rm $TLEDIR/noaa.txt
while [ 1 ]; do
    wget -r https://www.celestrak.com/NORAD/elements/noaa.txt -O $TLEDIR/noaa.txt --retry-connrefused --waitretry=1 --read-timeout=20 --timeout=15 -t 0 --continue
    if [ $? = 0 ]; then break; fi; # check return value, break if successful (0)
    /bin/sleep 1;
done;

echo `date`
echo Updated
