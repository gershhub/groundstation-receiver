#!/bin/bash

# a clunky way to use supervisor web interface to deploy: 
# pull the latest commit and copy everything to the production environment
sudo su slowimmediate << BASH
  cd /home/slowimmediate/pkg/groundstation-receiver;
  git reset --hard;
  git pull;
BASH

echo "Copying scripts to production environment... restart [program:groundstation] to deploy"
cp -r /home/slowimmediate/pkg/groundstation-receiver/* /usr/local/lib/groundstation-receiver/