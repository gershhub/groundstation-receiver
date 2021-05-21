#!/bin/bash
TLEDIR=/tmp

rm $TLEDIR/noaa.txt
wget -qr https://www.celestrak.com/NORAD/elements/noaa.txt -O $TLEDIR/noaa.txt

echo `date`
echo Updated