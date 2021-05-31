#!/bin/bash
TLEDIR=/home/slowimmediate/groundstation-data/TLE

rm $TLEDIR/noaa.txt
wget -qr https://www.celestrak.com/NORAD/elements/noaa.txt -O $TLEDIR/noaa.txt

echo `date`
echo Updated
