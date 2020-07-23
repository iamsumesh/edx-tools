"""

ABOUT

This is a tool to clean up invalid or unusable data in the "users" collection of
the cs_comments_service MongoDB instance.

The two main cases this script was created to solve are:
1) a user exists in the comments_service collection which does not exist in the lms MySQL database
2) a user exists in the comments_service collection whose username (or email) matches that of a
   *different* user in the lms database

These two cases overlap.  One common cause of both is legacy lms behavior, wherein the cs user is created
along with the lms user, but an error occurs later during lms user creation which triggers a rollback.


DEPENDENCIES

Setting up a virtualenv is recommended, followed by:

  pip install MySQL-python pymongo


USAGE 

The intended usage is:
1) `python clean_cs_users.py loadcs HOST PORT DB USER` which loads cs user data to a local sqlite database.
2) `python clean_cs_users.py loadlms HOST PORT DB USER` which loads lms user data to the same local sqlite database.
3) `python clean_cs_users.py check > CSV_FILE` discovers and dumps cs users that should be deleted.
4) `python clean_cs_users.py fix > JS_FILE` generates a script of MongoDB remove() calls for the cs users that should be deleted.

and then...

5) (execute the generated JS_FILE against your MongoDB instance to delete the problematic users.)


NOTES

 * Steps 1 and 2 can be repeated if necessary (but see warning below about the order in which they are executed.)
 * Read-only connections for the user downloads are allowed (and advised).
 * The populated sqlite database is named `clean_cs_users.db` and can be queried directly if desired.
 * Step 3 is completely optional, but provides a relatively user-friendly way to review the problematic data before acting.
 * For large data sets, you may want to create the following indices on your sqlite database:
    * create unique index lms_user_id on lms_user(id);
    * create unique index lms_user_username on lms_user(username);
    * create unique index cs_user_username on cs_user(username);
    * create unique index cs_user_email on cs_user(email);
    * create unique index cs_user_external_id on cs_user(external_id);

WARNINGS

It is strongly recommended to run loadcs *after* running loadlms, in order to avoid false-positives (if loadcs is run
second, it will pick up users created on the lms between the two runs, and conclude they are orphaned).  
This warning is also relevant in the case of using a stale copy or replica for the lms source database, if it is
of a less recent freshness than the cs database.

The sqlite database generated by this program, along with the csv and js outputs, contain personally identifiable
information and therefore should be securely deleted immediately after use.  

Aside from some rudimentary sanity-checking, there is nothing but your own diligence to ensure that the chosen lms
database and cs database belong to the same edx-platform instance/environment.

If a user identified for deletion appears to have previous activity in the forums, a warning will be logged
and no delete statement will be generated.


"""

from __future__ import absolute_import
from __future__ import print_function
from collections import namedtuple
import csv
from getpass import getpass
import json
import logging
import sqlite3
import sys

import MySQLdb
import MySQLdb.cursors as cursors
import pymongo

FETCHMANY_SIZE=10000

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s: %(message)s')
logger = logging.getLogger('')

JoinedUser = namedtuple('JoinedUser', 'cs_id cs_username cs_email cs_read_count lms_id lms_username lms_email')

def _drop_sqlite_table(sqlite_cx, name):
    """
    """
    try:
        sqlite_cx.execute("DROP TABLE {}".format(name))
    except sqlite3.OperationalError:
        pass

def load_lms_users(mysql_cx, sqlite_cx):
    """
    """
    logger.info("beginning to load lms_user data...")
    _drop_sqlite_table(sqlite_cx, 'lms_user')
    sqlite_cx.execute("CREATE TABLE lms_user (id INTEGER, username TEXT, email TEXT)")
    insert_sql = "INSERT INTO lms_user VALUES (?, ?, ?)"

    cur = mysql_cx.cursor()
    cur.execute("SELECT id, username, email FROM auth_user");

    cnt = 0
    while 1:
        rows = cur.fetchmany(FETCHMANY_SIZE)
        if not rows:
            sqlite_cx.commit()
            logger.info("done loading lms_user data")
            break
        sqlite_cx.executemany(insert_sql, rows)
        cnt += len(rows)
        logger.info("loaded {} rows".format(cnt))

def load_cs_users(mongo_db, sqlite_cx):
    """
    """
    logger.info("beginning to load cs_user data...")
    _drop_sqlite_table(sqlite_cx, 'cs_user')
    sqlite_cx.execute("CREATE TABLE cs_user (external_id INTEGER, username TEXT, email TEXT, read_count INT)")
    insert_sql = "INSERT INTO cs_user VALUES (?, ?, ?, ?)"
    cnt = 0
    for user in mongo_db.users.find({}, fields=['external_id', 'username', 'email', 'read_states']):
        read_count = len(user.get('read_states', []))
        sqlite_cx.execute(insert_sql, (int(user['external_id']), user['username'], user['email'], read_count))
        cnt += 1 
        if cnt % 1000 == 0:
            logger.info("loaded {} rows".format(cnt))
    sqlite_cx.commit()
    logger.info("done loading cs_user data")

def sanity_check(sqlite_cx):
    """
    """
    cur = sqlite_cx.cursor() 
    cur.execute('SELECT count(*) FROM lms_user')
    lms_user_count = int(cur.fetchone()[0])
    assert lms_user_count > 0
    cur.execute('SELECT count(*) FROM cs_user')
    cs_user_count = int(cur.fetchone()[0])
    assert cs_user_count > 0
    ratio = float(lms_user_count)/float(cs_user_count)
    assert 0.75 < ratio < 1.33

def get_orphaned_cs_users(sqlite_cx):
    """
    """
    logger.info('checking for orphaned cs users...')
    cur = sqlite_cx.cursor() 
    cur.execute("""
        SELECT c.*, l.*
        FROM cs_user c 
        LEFT JOIN lms_user l ON c.external_id = l.id
        WHERE l.id IS NULL
        """)
    ret = [JoinedUser(*v) for v in cur.fetchall()]
    logger.info('found {} orphaned users'.format(len(ret)))
    return ret

def get_conflicted_cs_users(sqlite_cx):
    """
    """
    logger.info('checking for conflicted cs users...')
    cur = sqlite_cx.cursor() 
    cur.execute("""
        SELECT c.*, l.*
        FROM lms_user l, cs_user c
        WHERE (c.username = l.username OR c.email = l.email)
        AND c.external_id != l.id
        AND EXISTS (SELECT 1 FROM lms_user WHERE id = c.external_id)
        """)
    ret = [JoinedUser(*v) for v in cur.fetchall()]
    logger.info('found {} conflicted users'.format(len(ret)))
    return ret

def dump_csv(users, f):
    logger.info('dumping to csv...')
    w = csv.writer(f)
    w.writerow(JoinedUser._fields)
    for u in users:
        w.writerow(u)

def dump_cs_deletes(users, f):
    logger.info('generating delete statements...')
    for u in users:
        print('db.users.remove({{_id: "{}"}}) // {}'.format(u.cs_id, u), file=f)


if __name__=='__main__':
    
    sqlite_cx = sqlite3.connect('clean_cs_users.db')
    sqlite_cx.text_factory = str

    if sys.argv[1] == 'loadlms':
        try:
            host, port, db, user = sys.argv[2:]
        except:
            logging.error('expected syntax: {} loadlms HOST PORT DB USER'.format(sys.argv[0]))
            sys.exit(1)
        load_lms_users(
            MySQLdb.connect(
                host=host,
                port=int(port),
                passwd=getpass('enter MySQL password for user {}: '.format(user)),
                user=user,
                db=db,
                cursorclass=cursors.SSCursor),
            sqlite_cx
        )
    elif sys.argv[1] == 'loadcs':
        try:
            host, port, db, user = sys.argv[2:]
        except:
            logging.error('expected syntax: {} loadcs HOST PORT DB USER'.format(sys.argv[0]))
            sys.exit(1)
        mongo_db = pymongo.MongoClient(
            '{}:{}'.format(host, port),
            slave_okay=True 
            )[db]
        mongo_db.authenticate(user, getpass('enter MongoDB password for user {}: '.format(user)))
        load_cs_users(
            mongo_db,
            sqlite_cx
        )
    elif sys.argv[1] == 'check':
        sanity_check(sqlite_cx)
        o_users = get_orphaned_cs_users(sqlite_cx)
        c_users = get_conflicted_cs_users(sqlite_cx)
        dump_csv(o_users + c_users, sys.stdout)
    elif sys.argv[1] == 'fix':
        sanity_check(sqlite_cx)
        o_users = get_orphaned_cs_users(sqlite_cx)
        c_users = get_conflicted_cs_users(sqlite_cx)
        del_users = []
        for u in (o_users + c_users):
            if u.cs_read_count != 0:
                logger.warning('Skipping {} due to previous forum activity.  Please resolve this instance manually.'.format(u))
            else:
                del_users.append(u)
        dump_cs_deletes(del_users, sys.stdout)

 
