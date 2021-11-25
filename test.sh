#!/bin/bash

echo "Sending jobs."
python client.py "add" --cmd "python gpu_check.py" --env "deep"
python client.py "add" --cmd "ping -c 4 www.google.com"
RET=$(python client.py "add" --cmd "ping -c 6 www.google.com")
echo "$RET"
TOCANCEL=$(echo "$RET" | jq '.id') # get if from json response
python client.py "add" --cmd "ping -c 5 www.google.com"
echo "Jobs sent."
sleep 0.5
python client.py "list"
echo "Cancelling $TOCANCEL."
python client.py "cancel" --id "$TOCANCEL"
python client.py "list"
echo "Killing running job."
python client.py "kill"
python client.py "list"
