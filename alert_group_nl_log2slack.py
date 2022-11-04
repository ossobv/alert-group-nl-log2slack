#!/usr/bin/env python3
import datetime
import json
import os
import time
import sys
from contextlib import suppress
from hashlib import md5
from traceback import print_exc

from bs4 import BeautifulSoup
import requests


KLANT_NUMMER = os.environ['KLANT_NUMMER']
KLANT_CODE = os.environ['KLANT_CODE']
KLANT_GECRYPT = md5(KLANT_CODE.encode('ascii')).hexdigest()
SLACK_WEBHOOK_URL = os.environ['SLACK_WEBHOOK_URL']
CACHE_FILENAME = (__file__.rsplit('.py', 1)[0] + '.cache')


def send_slack_message(message):
    ret = requests.post(
        SLACK_WEBHOOK_URL, data=json.dumps({'text': message}),
        headers={'Content-Type': 'application/json'})
    assert ret.status_code == 200, (ret, ret.text)


def login_and_fetch(klant_nummer, klant_gecrypt):
    # Session keeps cookies around.
    with requests.Session() as session:
        ret = session.get('https://alertmobile.alert-group.nl/koi_kb.php')
        assert ret.status_code == 200, (ret, ret.text)

        ret = session.post(
            'https://alertmobile.alert-group.nl/koi_kb.php', data={
                'klantnr': klant_nummer, 'klantcode': '',
                'gecrypt': klant_gecrypt})
        assert ret.status_code == 200, (ret, ret.text)

        for attempt in range(10):
            ret = session.get(
                'https://alertmobile.alert-group.nl/koi_kb.php'
                '?mscherm=status&div=historie')
            assert ret.status_code == 200, (ret, ret.text)

            if 'Recent ontvangen meldingen:' in ret.text:
                break
            elif 'koi_kb.php?mscherm=gebruiker_wijzigen' in ret.text:
                # Old data? Need to call the status screen at least one
                # second time. Not sure if it's because we "have to go
                # through another page" or if it's a timing thing.
                time.sleep(0.3)
            else:
                break

        assert 'Recent ontvangen meldingen:', (ret, ret.text)
    return ret.text


def html_table_to_dicts(html_doc):
    soup = BeautifulSoup(html_doc, 'html.parser')

    table = soup.find_all('table')[0]
    columns = [i.text for i in table.thead.tr.find_all('th')]
    data = []
    for tr in table.tbody.find_all('tr'):
        data.append(dict(
            (columns[n], i.text.strip())
            for n, i in enumerate(tr.find_all('td'))))

    return data


def fix_dicts_datetime(data):
    """
    The dict has the date as an event, and then all events. Merge the
    dates into the times.

    [{'Aansluiting': '',
      'Alrm': '',
      'Groep': '',
      'Omschrijving': '',
      'Sector': '---',
      'Tijd': '01/11/22'},
     {'Aansluiting': 'E0123456',
      'Alrm': 'INF',
      'Groep': '',
      'Omschrijving': 'AUTOTEST',
      'Sector': '0',
      'Tijd': '10:11:40'}]
    """
    new_data = []
    date = None
    for row in data:
        if (row['Aansluiting'] == row['Alrm'] == row['Groep']
                == row['Omschrijving'] == '' and row['Sector'] == '---'):
            dd, mm, yy = row['Tijd'].split('/')
            date = datetime.date(2000 + int(yy), int(mm), int(dd))
        else:
            hh, mm, ss = row['Tijd'].split(':')
            # datetimes are TZ agnostic (= localtime)
            row['Tijd'] = datetime.datetime(
                date.year, date.month, date.day, int(hh), int(mm), int(ss))
            new_data.append(row)
    return new_data


def fix_dicts_who_did_what(data):
    """
    The dict has info in a higher up event.

    [{'Aansluiting': 'E0123456',
      'Alrm': 'INF',
      'Groep': '6',
      'Omschrijving': 'UITGESCH. JOHN',
      'Sector': '0',
      'Tijd': datetime.datetime(2001, 11, 22, 8, 45, 27)},
     {'Aansluiting': 'E0123456',
      'Alrm': 'UIT',
      'Groep': '6',
      'Omschrijving': 'Uit',
      'Sector': '0',
      'Tijd': datetime.datetime(2001, 11, 22, 8, 45, 27)}]
    """
    new_data = []
    info = None
    for row in data:
        if row['Alrm'] == 'INF':
            info = row
        elif info is not None:
            assert row['Aansluiting'] == info['Aansluiting'], (row, info)
            assert row['Groep'] == info['Groep'], (row, info)
            assert row['Sector'] == info['Sector'], (row, info)
            assert row['Tijd'] == info['Tijd'], (row, info)
            row['Info'] = info['Omschrijving']
            info = None
            new_data.append(row)
        else:
            new_data.append(row)
    return new_data


def filter_only_on_off(data):
    return [i for i in data if i['Alrm'] in ('IN', 'UIT')]


def to_strings(data):
    new_data = []
    assert all(i['Aansluiting'] == data[0]['Aansluiting'] for i in data), data
    for row in data:
        event = {'IN': 'ALARM_ON', 'UIT': 'ALARM_OFF'}.get(
            row['Alrm'], row['Alrm'])
        group = row['Groep']
        sector = row['Sector']
        datetime = row['Tijd'].strftime('%Y-%m-%d %H:%M:%S')
        extra = row['Info'] if 'Info' in row else ''
        new_data.append(
            f'{datetime}: {event} (G{group}/S{sector}): {extra}'.rstrip())
    return new_data


def fetch(klant_nummer, klant_gecrypt):
    try:
        with open(CACHE_FILENAME) as fp:
            data = fp.read()
        assert 'Recent ontvangen meldingen:' in data
    except Exception:
        data = login_and_fetch(klant_nummer, klant_gecrypt)
        with open(CACHE_FILENAME, 'w') as fp:
            fp.write(data)
    return data


def fetch_logs():
    data = fetch(KLANT_NUMMER, KLANT_GECRYPT)
    data = html_table_to_dicts(data)
    data = fix_dicts_datetime(data)
    data = fix_dicts_who_did_what(data)
    #data = filter_only_on_off(data)
    data = to_strings(data)
    #os.unlink(CACHE_FILENAME)
    return data


def fetch_logs_with_retry():
    max_try_time = 1800  # 30 mins
    t0 = time.time()

    while True:
        try:
            return fetch_logs()
        except Exception:
            td = time() - t0
            if td >= max_try_time:
                raise
            print_exc()
            print()
            print('Sleeping 180...')
            time.sleep(180)
    raise NotImplementedError()


def fetch_logs_and_publish_forever():
    already_published = set()

    while True:
        with suppress(FileNotFoundError):
            os.unlink(CACHE_FILENAME)

        data = set(fetch_logs_with_retry())
        not_published_yet = (data - already_published)
        print('data:', len(data), 'new:', not_published_yet)
        already_published = data

        a_while_ago = (datetime.datetime.now() - datetime.timedelta(hours=4))
        for row in sorted(not_published_yet):
            if row < a_while_ago.strftime('%Y-%m-%d %H:%M:%S'):
                print('skipping old:', row)
            else:
                send_slack_message(row)
                print('sent message:', row)

        time.sleep(300)


if sys.argv[1:2] == ['publish']:
    fetch_logs_and_publish_forever()
else:
    for row in sorted(fetch_logs()):
        print(row)
