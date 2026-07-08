#!/bin/bash

# Programs
CURL="$(which curl )"

# Variables
TOKEN=$1

cat toUpdate.txt | while read line;
do
  OUTPUT=$(curl --silent -X PUT "https://api.proxy.chris.com/api/v1/machines" -H "Accept: application/json" 
  -H "Access-Token: $TOKEN" -H "Content-Type: application/json" -d "{ \"upgrade_now\": false }")
  echo $OUTPUT
done
