"""Debug client for the SAME leased web workflow; never owns the serial port.

Set SPARK_CONTROL_TOKEN; invoke with --url http://host:8768 --start only
after the fixed layout is physically commissioned and the site is ready.
Without --start this is read-only. No lock clearing or automatic retries.
"""
import argparse
import json
import os
import time
import uuid
import requests


def start_payload(state, operator):
    return dict(action='start',session=state['session'],revision=state['revision'],
                operator=operator,checked=True,levels=3,structure='tower3',automatic=True)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--url',required=True)
    parser.add_argument('--start',action='store_true')
    args=parser.parse_args()
    root=args.url.rstrip('/')+'/api/fixed-tower'
    client=requests.Session()
    state=client.get(root+'/state',timeout=2).json()
    if not args.start:
        print(json.dumps(state,ensure_ascii=False,indent=2));return
    token=os.environ['SPARK_CONTROL_TOKEN']
    operator=uuid.uuid4().hex
    headers={'X-Control-Token':token}
    def post(path,body):
        response=client.post(root+path,json=body,headers=headers,timeout=.8)
        response.raise_for_status()
        return response.json()
    active=False
    try:
        post('/heartbeat',start_payload(state,operator))
        active=True  # A timed-out start may already have reached the server.
        state=post('/control',start_payload(state,operator))
        previous=None
        while state['phase'] not in ('completed','stopped'):
            post('/heartbeat',start_payload(state,operator))
            response=client.get(root+'/state',timeout=.8);response.raise_for_status();state=response.json()
            progress=(state['phase'],state['placed'],(state.get('agent') or {}).get('progress'))
            if progress!=previous:
                print(json.dumps(dict(phase=progress[0],placed=progress[1],progress=progress[2]),ensure_ascii=False),flush=True)
                previous=progress
            time.sleep(.2)
        active=False
        print(json.dumps(dict(phase=state['phase'],placed=state['placed'],error=state.get('error')),ensure_ascii=False))
    finally:
        if active:
            try:
                body=start_payload(state,operator);body['action']='stop';post('/control',body)
            except requests.RequestException:
                pass  # Independent robot lease/watchdog stops without this client.


if __name__=='__main__':
    main()
