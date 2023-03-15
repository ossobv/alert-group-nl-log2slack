#!/usr/bin/env python3
import datetime
import json
import os
import time
import sys
from base64 import b64decode
from collections import namedtuple
from contextlib import suppress
from hashlib import md5
from traceback import print_exc

from bs4 import BeautifulSoup
import phpserialize
import requests


KLANT_NUMMER = os.environ.get('KLANT_NUMMER')
KLANT_CODE = os.environ.get('KLANT_CODE', '')
KLANT_GECRYPT = md5(KLANT_CODE.encode('ascii')).hexdigest()
SLACK_WEBHOOK_URL = os.environ.get('SLACK_WEBHOOK_URL')
CACHE_FILENAME = (__file__.rsplit('.py', 1)[0] + '.cache')

ALERTMOBILE_URL = 'https://alertmobile.alert-group.nl/koi_kb.php'
MAX_FAIL_TIME = 1800
SLEEP_AFTER_FETCH = 300
SLEEP_AFTER_FAIL = 180


class AlarmRecord(
        namedtuple('AlarmRecord', 'datetime event group sector extra')):
    SORT_KEY = (lambda x: (x.datetime, x.event))
    NORMAL_EVENTS = ('ALARM_ON', 'ALARM_OFF', '24H')

    @property
    def datetime_str(self):
        return self.datetime.strftime('%Y-%m-%d %H:%M:%S')

    def __str__(self):
        ret = (
            '{0.datetime_str}: {0.event} (G{0.group}/S{0.sector}): {0.extra}'
            .format(self).rstrip())
        if self.event not in self.NORMAL_EVENTS:
            ret += ' <-- <!channel>'  # slack notification
        return ret

    def __repr__(self):
        return repr(str(self))


def send_slack_message(message):
    ret = requests.post(
        SLACK_WEBHOOK_URL, data=json.dumps({'text': message}),
        headers={'Content-Type': 'application/json'})
    assert ret.status_code == 200, (ret, ret.text)


def from_utf8(data):
    if isinstance(data, bytes):
        return data.decode('utf-8')
    if isinstance(data, list):
        return [from_utf8(i) for i in data]
    if isinstance(data, dict):
        return dict((from_utf8(k), from_utf8(v)) for k, v in data.items())
    return data


def decode_cookie(val):
    decoding = []

    try:
        val = b64decode(val)
        decoding.append('b64')
    except ValueError:
        try:
            val = b64decode(val.replace('%3D', '='))
            decoding.append('b64pct')
        except ValueError:
            pass

    try:
        val = json.dumps(
            from_utf8(phpserialize.loads(
                val, object_hook=(lambda k, v: {k: dict(v)}))),
            separators=(', ', ':'))  # semi-compact
        decoding.append('phpser')
    except Exception:
        try:
            val = val.decode('utf-8')
            decoding.append('utf8')
        except UnicodeDecodeError:
            pass

    if not decoding:
        decoding = ['raw']
    return ';'.join(decoding), val


def dump_cookies(session, where):
    # oa-koi-kb, has what looks to be a php-serialized value like:
    # '''O:13:"koi_kb_config":26:{s:6:"access";i:3;s:8:"sessienr";s:11:...'''
    # (base64 + '=' escaped as '%3D')
    for key, value in session.cookies.items():
        type_, decoded = decode_cookie(value)
        print(f'cookies @ {where}: {key} ({type_}) = {decoded}')


def login_and_fetch(klant_nummer, klant_gecrypt):
    # Session keeps cookies around.
    with requests.Session() as session:
        ret = session.get(ALERTMOBILE_URL)
        dump_cookies(session, 'first get')
        assert ret.status_code == 200, (ret, ret.text)

        ret = session.post(ALERTMOBILE_URL, data={
                'klantnr': klant_nummer, 'klantcode': '',
                'gecrypt': klant_gecrypt})
        dump_cookies(session, 'login post')
        assert ret.status_code == 200, (ret, ret.text)

        for attempt in range(10):
            ret = session.get(f'{ALERTMOBILE_URL}?mscherm=status&div=historie')
            dump_cookies(session, 'status get')
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
    The dict has info in a higher up event. Or sometimes lower..

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
    last_row = None
    for row in data:
        if row['Alrm'] == 'INF':
            if last_row is not None and (
                    last_row['Aansluiting'] == row['Aansluiting'] and
                    last_row['Groep'] == row['Groep'] and
                    last_row['Sector'] == row['Sector'] and
                    last_row['Tijd'] == row['Tijd']):
                last_row['Info'] = row['Omschrijving']
            else:
                info = row
        elif info is not None:
            assert row['Aansluiting'] == info['Aansluiting'], (row, info)
            assert row['Groep'] == info['Groep'], (row, info)
            assert row['Sector'] == info['Sector'], (row, info)
            assert row['Tijd'] == info['Tijd'], (row, info)
            row['Info'] = info['Omschrijving']
            info = None
            new_data.append(row)
            last_row = None
        else:
            new_data.append(row)
            last_row = row
    return new_data


def to_records(data):
    new_data = []
    assert all(i['Aansluiting'] == data[0]['Aansluiting'] for i in data), data
    for row in data:
        event = {
                'IN': 'ALARM_ON',
                'UIT': 'ALARM_OFF',
                '24H': '24H',                       # ?, always "AUTOTEST"
                # The following concern the known times in which the
                # alarm may be switched on/off.
                'TVU': 'UNEXPECT_ALARM_OFF',        # "Te Vroeg Uitgeschakeld"
                'TLI': 'UNEXPECT_NO_ALARM_YET',     # "Te Laat Ingeschakeld"
                'AFW': 'OVERRIDE_ALARM_TIME',       # Afwijkende tijd(?)
            }.get(
            row['Alrm'], row['Alrm'])

        extra = row['Info'] if 'Info' in row else ''
        if row['Omschrijving']:
            if extra and extra != ':':
                extra += f' ({row["Omschrijving"]})'
            else:
                extra = row['Omschrijving']

        new_data.append(AlarmRecord(
            datetime=row['Tijd'], event=(event or '(log)'), group=row['Groep'],
            sector=row['Sector'], extra=extra))
    return new_data


def fetch(klant_nummer, klant_gecrypt):
    try:
        with open(CACHE_FILENAME) as fp:
            data = fp.read()
    except Exception:
        # Leave exception handler so we won't see this as cause later on.
        data = ''

    if 'Recent ontvangen meldingen:' not in data:
        data = login_and_fetch(klant_nummer, klant_gecrypt)
        with open(CACHE_FILENAME, 'w') as fp:
            fp.write(data)

    return data


def fetch_logs():
    data = fetch(KLANT_NUMMER, KLANT_GECRYPT)
    data = html_table_to_dicts(data)
    data = fix_dicts_datetime(data)
    data = fix_dicts_who_did_what(data)
    data = to_records(data)
    # data = [i for i in data if i.event in ('ALARM_ON', 'ALARM_OFF')]
    # os.unlink(CACHE_FILENAME)
    return data


def fetch_logs_with_retry():
    t0 = time.time()
    while True:
        with suppress(FileNotFoundError):
            os.unlink(CACHE_FILENAME)
        try:
            return fetch_logs()
        except Exception:
            td = time.time() - t0
            if td >= MAX_FAIL_TIME:
                raise
            print_exc()
            print(f'# retrying after {SLEEP_AFTER_FAIL}')
            time.sleep(SLEEP_AFTER_FAIL)
    raise NotImplementedError()


def fetch_logs_and_publish_forever():
    already_published = set()

    while True:
        data = set(fetch_logs_with_retry())
        not_published_yet = (data - already_published)
        print(f'data count: {len(data)}, new: {not_published_yet}')
        already_published = data

        a_while_ago = (datetime.datetime.now() - datetime.timedelta(hours=4))
        for record in sorted(not_published_yet, key=AlarmRecord.SORT_KEY):
            if record.datetime < a_while_ago:
                print(f'skipping old: {record}')
            else:
                send_slack_message(str(record))
                print(f'sent message: {record}')

        time.sleep(300)


def test():
    """
    Hide the tests inside this function. Only load/parse this when called.
    """
    import unittest

    class AllTests(unittest.TestCase):
        maxDiff = None

        def test_html_table_to_dicts(self):
            with open('test_status_1.html') as fp:
                data = fp.read()
            data = html_table_to_dicts(data)
            expected_data = [
               {'Aansluiting': '',
                'Alrm': '',
                'Groep': '',
                'Omschrijving': '',
                'Sector': '---',
                'Tijd': '03/02/23'},
               {'Aansluiting': 'E0123456',
                'Alrm': 'INF',
                'Groep': '',
                'Omschrijving': 'AUTOTEST',
                'Sector': '0',
                'Tijd': '10:12:05'},
               {'Aansluiting': 'E0123456',
                'Alrm': '24H',
                'Groep': '',
                'Omschrijving': 'Test',
                'Sector': '0',
                'Tijd': '10:12:05'},
               {'Aansluiting': 'E0123456',
                'Alrm': 'UIT',
                'Groep': '6',
                'Omschrijving': 'Uit',
                'Sector': '0',
                'Tijd': '08:37:20'},
               {'Aansluiting': 'E0123456',
                'Alrm': 'INF',
                'Groep': '6',
                'Omschrijving': 'UITGESCH. CHARLIE',
                'Sector': '0',
                'Tijd': '08:37:20'},
               {'Aansluiting': '',
                'Alrm': '',
                'Groep': '',
                'Omschrijving': '',
                'Sector': '---',
                'Tijd': '02/02/23'},
               {'Aansluiting': 'E0123456',
                'Alrm': 'INF',
                'Groep': '5',
                'Omschrijving': 'VOLL. ING BOB',
                'Sector': '0',
                'Tijd': '19:09:48'},
               {'Aansluiting': 'E0123456',
                'Alrm': 'IN',
                'Groep': '5',
                'Omschrijving': 'In',
                'Sector': '0',
                'Tijd': '19:09:48'},
               {'Aansluiting': 'E0123456',
                'Alrm': 'INF',
                'Groep': '',
                'Omschrijving': 'AUTOTEST',
                'Sector': '0',
                'Tijd': '10:12:05'},
               {'Aansluiting': 'E0123456',
                'Alrm': '24H',
                'Groep': '',
                'Omschrijving': 'Test',
                'Sector': '0',
                'Tijd': '10:12:05'},
               {'Aansluiting': 'E0123456',
                'Alrm': 'INF',
                'Groep': '1',
                'Omschrijving': 'UITGESCH. ALICE',
                'Sector': '0',
                'Tijd': '08:37:11'},
               {'Aansluiting': 'E0123456',
                'Alrm': 'UIT',
                'Groep': '1',
                'Omschrijving': 'Uit',
                'Sector': '0',
                'Tijd': '08:37:11'},
            ]
            self.assertEqual(expected_data, data)

            data = fix_dicts_datetime(data)
            expected_data = [
                {'Aansluiting': 'E0123456',
                 'Alrm': 'INF',
                 'Groep': '',
                 'Omschrijving': 'AUTOTEST',
                 'Sector': '0',
                 'Tijd': datetime.datetime(2023, 2, 3, 10, 12, 5)},
                {'Aansluiting': 'E0123456',
                 'Alrm': '24H',
                 'Groep': '',
                 'Omschrijving': 'Test',
                 'Sector': '0',
                 'Tijd': datetime.datetime(2023, 2, 3, 10, 12, 5)},
                {'Aansluiting': 'E0123456',
                 'Alrm': 'UIT',
                 'Groep': '6',
                 'Omschrijving': 'Uit',
                 'Sector': '0',
                 'Tijd': datetime.datetime(2023, 2, 3, 8, 37, 20)},
                {'Aansluiting': 'E0123456',
                 'Alrm': 'INF',
                 'Groep': '6',
                 'Omschrijving': 'UITGESCH. CHARLIE',
                 'Sector': '0',
                 'Tijd': datetime.datetime(2023, 2, 3, 8, 37, 20)},
                {'Aansluiting': 'E0123456',
                 'Alrm': 'INF',
                 'Groep': '5',
                 'Omschrijving': 'VOLL. ING BOB',
                 'Sector': '0',
                 'Tijd': datetime.datetime(2023, 2, 2, 19, 9, 48)},
                {'Aansluiting': 'E0123456',
                 'Alrm': 'IN',
                 'Groep': '5',
                 'Omschrijving': 'In',
                 'Sector': '0',
                 'Tijd': datetime.datetime(2023, 2, 2, 19, 9, 48)},
                {'Aansluiting': 'E0123456',
                 'Alrm': 'INF',
                 'Groep': '',
                 'Omschrijving': 'AUTOTEST',
                 'Sector': '0',
                 'Tijd': datetime.datetime(2023, 2, 2, 10, 12, 5)},
                {'Aansluiting': 'E0123456',
                 'Alrm': '24H',
                 'Groep': '',
                 'Omschrijving': 'Test',
                 'Sector': '0',
                 'Tijd': datetime.datetime(2023, 2, 2, 10, 12, 5)},
                {'Aansluiting': 'E0123456',
                 'Alrm': 'INF',
                 'Groep': '1',
                 'Omschrijving': 'UITGESCH. ALICE',
                 'Sector': '0',
                 'Tijd': datetime.datetime(2023, 2, 2, 8, 37, 11)},
                {'Aansluiting': 'E0123456',
                 'Alrm': 'UIT',
                 'Groep': '1',
                 'Omschrijving': 'Uit',
                 'Sector': '0',
                 'Tijd': datetime.datetime(2023, 2, 2, 8, 37, 11)},
            ]
            self.assertEqual(expected_data, data)

            data = fix_dicts_who_did_what(data)
            expected_data = [
                {'Aansluiting': 'E0123456',
                 'Alrm': '24H',
                 'Groep': '',
                 'Info': 'AUTOTEST',
                 'Omschrijving': 'Test',
                 'Sector': '0',
                 'Tijd': datetime.datetime(2023, 2, 3, 10, 12, 5)},
                {'Aansluiting': 'E0123456',
                 'Alrm': 'UIT',
                 'Groep': '6',
                 'Info': 'UITGESCH. CHARLIE',
                 'Omschrijving': 'Uit',
                 'Sector': '0',
                 'Tijd': datetime.datetime(2023, 2, 3, 8, 37, 20)},
                {'Aansluiting': 'E0123456',
                 'Alrm': 'IN',
                 'Groep': '5',
                 'Info': 'VOLL. ING BOB',
                 'Omschrijving': 'In',
                 'Sector': '0',
                 'Tijd': datetime.datetime(2023, 2, 2, 19, 9, 48)},
                {'Aansluiting': 'E0123456',
                 'Alrm': '24H',
                 'Groep': '',
                 'Info': 'AUTOTEST',
                 'Omschrijving': 'Test',
                 'Sector': '0',
                 'Tijd': datetime.datetime(2023, 2, 2, 10, 12, 5)},
                {'Aansluiting': 'E0123456',
                 'Alrm': 'UIT',
                 'Groep': '1',
                 'Info': 'UITGESCH. ALICE',
                 'Omschrijving': 'Uit',
                 'Sector': '0',
                 'Tijd': datetime.datetime(2023, 2, 2, 8, 37, 11)},
            ]
            self.assertEqual(expected_data, data)

            data = to_records(data)
            expected_data = [
                AlarmRecord(
                    datetime=datetime.datetime(2023, 2, 3, 10, 12, 5),
                    event='24H', group='', sector='0',
                    extra='AUTOTEST (Test)'),
                AlarmRecord(
                    datetime=datetime.datetime(2023, 2, 3, 8, 37, 20),
                    event='ALARM_OFF', group='6', sector='0',
                    extra='UITGESCH. CHARLIE (Uit)'),
                AlarmRecord(
                    datetime=datetime.datetime(2023, 2, 2, 19, 9, 48),
                    event='ALARM_ON', group='5', sector='0',
                    extra='VOLL. ING BOB (In)'),
                AlarmRecord(
                    datetime=datetime.datetime(2023, 2, 2, 10, 12, 5),
                    event='24H', group='', sector='0',
                    extra='AUTOTEST (Test)'),
                AlarmRecord(
                    datetime=datetime.datetime(2023, 2, 2, 8, 37, 11),
                    event='ALARM_OFF', group='1', sector='0',
                    extra='UITGESCH. ALICE (Uit)'),
            ]
            self.assertEqual(expected_data, data)

            # data = [i for i in data if i.event in ('ALARM_ON', 'ALARM_OFF')]
            # os.unlink(CACHE_FILENAME)
            return data

        def test_html_table_to_dicts_ii(self):
            with open('test_status_2.html') as fp:
                data = fp.read()

            data = html_table_to_dicts(data)
            data = fix_dicts_datetime(data)
            data = fix_dicts_who_did_what(data)
            data = to_records(data)

            expected_data = [
                # vvvv-- maybe these should be merged into a single one -vvvv
                AlarmRecord(
                    datetime=datetime.datetime(2023, 3, 15, 13, 54, 51),
                    event='(log)', group='', sector='0',
                    extra='Gebr.: Mark Evert Chaniciën'),
                AlarmRecord(
                    datetime=datetime.datetime(2023, 3, 15, 13, 54, 51),
                    event='(log)', group='', sector='0',
                    extra='Resultaat : Test volgens plan verlopen'),
                AlarmRecord(
                    datetime=datetime.datetime(2023, 3, 15, 13, 54, 51),
                    event='Y-M', group='', sector='0',
                    extra='Einde test Monteur'),
                # ^^^^-- maybe these should be merged into a single one -^^^^
                AlarmRecord(
                    datetime=datetime.datetime(2023, 3, 15, 13, 15,23),
                    event='LPE', group='', sector='0',
                    extra='-INSTALL. (Einde lokale progr.)'),
                AlarmRecord(
                    datetime=datetime.datetime(2023, 3, 15, 13, 14, 45),
                    event='24H', group='99', sector='0',
                    extra='INST TEST INST. (Test)'),
                AlarmRecord(
                    datetime=datetime.datetime(2023, 3, 15, 12, 14, 44),
                    event='ALARM_OFF', group='98', sector='0',
                    extra='ALM RESET MANAGR (Uit na alarm)'),
                AlarmRecord(
                    datetime=datetime.datetime(2023, 3, 15, 12, 14, 41),
                    event='ALARM_OFF', group='98', sector='0',
                    extra='UITGESCH. MANAGR (Uit)'),
                AlarmRecord(
                    datetime=datetime.datetime(2023, 3, 15, 12, 14, 39),
                    event='HER', group='1034', sector='0',
                    extra='-INBRAAK   GBM RAAM KANTOOR (Herstel)'),
                AlarmRecord(
                    datetime=datetime.datetime(2023, 3, 15, 12, 14, 36),
                    event='RES', group='98', sector='0',
                    extra='ALARM RST MANAGR (Reset door gebruiker)'),
                AlarmRecord(
                    datetime=datetime.datetime(2023, 3, 15, 12, 14, 23),
                    event='INB', group='', sector='0',
                    extra='RECENT IN (Alarm binnen 5 min. na In)'),
                AlarmRecord(
                    datetime=datetime.datetime(2023, 3, 15, 12, 14, 20),
                    event='INB', group='1034', sector='0',
                    extra='INBRAAK   GBM RAAM KANTOOR (Inbraak)'),
                AlarmRecord(
                    datetime=datetime.datetime(2023, 3, 15, 12, 13, 50),
                    event='ALARM_ON', group='99', sector='0',
                    extra='VOLL. ING INST. (In)'),
                AlarmRecord(
                    datetime=datetime.datetime(2023, 3, 15, 12, 0, 26),
                    event='LPB', group='', sector='0',
                    extra='+INSTALL. (Start Lokale progr.)'),
                # vvvv-- maybe these should be merged into a single one -vvvv
                AlarmRecord(
                    datetime=datetime.datetime(2023, 3, 15, 11, 48, 50),
                    event='(log)', group='', sector='0',
                    extra='Gebr.: Mark Evert Chaniciën'),
                AlarmRecord(
                    datetime=datetime.datetime(2023, 3, 15, 11, 48, 50),
                    event='(log)', group='', sector='0',
                    extra='Reden : Periodiek Onderhoud'),
                AlarmRecord(
                    datetime=datetime.datetime(2023, 3, 15, 11, 48, 50),
                    event='X-M', group='', sector='0',
                    extra='Begin test Monteur'),
                # ^^^^-- maybe these should be merged into a single one -^^^^
                AlarmRecord(
                    datetime=datetime.datetime(2023, 3, 15, 10, 12, 15),
                    event='24H', group='', sector='0',
                    extra='AUTOTEST (Test)'),
                AlarmRecord(
                    datetime=datetime.datetime(2023, 3, 15, 8, 42, 54),
                    event='ALARM_OFF', group='6', sector='0',
                    extra='UITGESCH. BOB (Uit)'),
                AlarmRecord(
                    datetime=datetime.datetime(2023, 3, 14, 18, 56, 5),
                    event='ALARM_ON', group='6', sector='0',
                    extra='VOLL. ING BOB (In)'),
                AlarmRecord(
                    datetime=datetime.datetime(2023, 3, 14, 10, 12, 16),
                    event='24H', group='', sector='0',
                    extra='AUTOTEST (Test)'),
                AlarmRecord(
                    datetime=datetime.datetime(2023, 3, 14, 8, 27, 42),
                    event='ALARM_OFF', group='14', sector='0',
                    extra='UITGESCH. ALICE (Uit)'),
            ]
            self.assertEqual(expected_data, data)

            return data

    # Returns a test suite with a single test class. This is then run by
    # unittest.main().
    return unittest.defaultTestLoader.loadTestsFromTestCase(AllTests)


if __name__ == '__main__':
    if sys.argv[1:2] == ['publish']:
        print('# alert_group_nl_log2slack')
        for varname in (
                'ALERTMOBILE_URL MAX_FAIL_TIME '
                'SLEEP_AFTER_FETCH SLEEP_AFTER_FAIL'.split()):
            value = globals()[varname]
            print(f'# - {varname} = {value}')
        fetch_logs_and_publish_forever()
    elif sys.argv[1:2] == ['test']:
        from unittest import main
        os.environ['KLANT_NUMMER'] = 'E123456'  # yes, without 0
        os.environ['KLANT_CODE'] = '<supersecretpasswordhere>'
        main()
    else:
        already_published = set()
        for record in sorted(fetch_logs(), key=AlarmRecord.SORT_KEY):
            print(record)
            already_published.add(record)

        for record in sorted(fetch_logs(), key=AlarmRecord.SORT_KEY):
            assert record in already_published, (record, already_published)
