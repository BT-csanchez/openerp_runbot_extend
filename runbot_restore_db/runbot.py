from openerp.addons.runbot import runbot
import psutil
import datetime
import fcntl
import glob
import hashlib
import itertools
import logging
import operator
import os
import re
import resource
import shutil
import signal
import simplejson
import socket
import subprocess
import sys
import time
from collections import OrderedDict

import dateutil.parser
import requests
from matplotlib.font_manager import FontProperties
from matplotlib.textpath import TextToPath
import werkzeug

import openerp
from openerp import http
from openerp.http import request
from openerp.osv import fields, osv
from openerp.tools import config, appdirs
from openerp.addons.website.models.website import slug
from openerp.addons.website_sale.controllers.main import QueryURL


loglevels = (('none', 'None'),
             ('warning', 'Warning'),
             ('error', 'Error'))

LOG_FILENAME = '/tmp/{0}'.format(__name__)

# Set up a specific logger with our desired output level
_logger = logging.getLogger(__name__)
_logger.setLevel(logging.DEBUG)

# Add the log message handler to the logger
handler = logging.handlers.RotatingFileHandler(
              LOG_FILENAME, maxBytes=20000, backupCount=10)

_logger.addHandler(handler)




_re_error = r'^(?:\d{4}-\d\d-\d\d \d\d:\d\d:\d\d,\d{3} \d+ (?:ERROR|CRITICAL) )|(?:Traceback \(most recent call last\):)$'
_re_warning = r'^\d{4}-\d\d-\d\d \d\d:\d\d:\d\d,\d{3} \d+ WARNING '
_re_job = re.compile('job_\d')

def log(*l, **kw):
    out = [i if isinstance(i, basestring) else repr(i) for i in l] + \
          ["%s=%r" % (k, v) for k, v in kw.items()]
    _logger.debug(' '.join(out))

def can_use_db_template(cr, db):
    cr.execute("""select datname FROM pg_stat_activity;""")
    datnames = [x[0] for x in cr.fetchall()]
    return db not in datnames

def dashes(string):
    """Sanitize the input string"""
    for i in '~":\'':
        string = string.replace(i, "")
    for i in '/_. ':
        string = string.replace(i, "-")
    return string

def mkdirs(dirs):
    for d in dirs:
        if not os.path.exists(d):
            os.makedirs(d)

def grep(filename, string):
    if os.path.isfile(filename):
        return open(filename).read().find(string) != -1
    return False

def rfind(filename, pattern):
    """Determine in something in filename matches the pattern"""
    if os.path.isfile(filename):
        regexp = re.compile(pattern, re.M)
        with open(filename, 'r') as f:
            if regexp.findall(f.read()):
                return True
    return False

def lock(filename):
    fd = os.open(filename, os.O_CREAT | os.O_RDWR, 0600)
    fcntl.lockf(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)

def locked(filename):
    result = False
    try:
        fd = os.open(filename, os.O_CREAT | os.O_RDWR, 0600)
        try:
            fcntl.lockf(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except IOError:
            result = True
        os.close(fd)
    except OSError:
        result = False
    return result

def nowait():
    signal.signal(signal.SIGCHLD, signal.SIG_IGN)

def run(l, env=None):
    """Run a command described by l in environment env"""
    log("run", l)
    env = dict(os.environ, **env) if env else None
    if isinstance(l, list):
        if env:
            rc = os.spawnvpe(os.P_WAIT, l[0], l, env)
        else:
            rc = os.spawnvp(os.P_WAIT, l[0], l)
    elif isinstance(l, str):
        tmp = ['sh', '-c', l]
        if env:
            rc = os.spawnvpe(os.P_WAIT, tmp[0], tmp, env)
        else:
            rc = os.spawnvp(os.P_WAIT, tmp[0], tmp)
    log("run", rc=rc)
    return rc

def now():
    return time.strftime(openerp.tools.DEFAULT_SERVER_DATETIME_FORMAT)

def dt2time(datetime):
    """Convert datetime to time"""
    return time.mktime(time.strptime(datetime, openerp.tools.DEFAULT_SERVER_DATETIME_FORMAT))

def s2human(time):
    """Convert a time in second into an human readable string"""
    for delay, desc in [(86400,'d'),(3600,'h'),(60,'m')]:
        if time >= delay:
            return str(int(time / delay)) + desc
    return str(int(time)) + "s"

def flatten(list_of_lists):
    return list(itertools.chain.from_iterable(list_of_lists))

def decode_utf(field):
    try:
        return field.decode('utf-8')
    except UnicodeDecodeError:
        return ''

def uniq_list(l):
    return OrderedDict.fromkeys(l).keys()

def fqdn():
    return socket.getfqdn()

class runbot_build(osv.osv):
    _inherit = "runbot.build"

    def job_21_checkdeadbuild(self, cr, uid, build, lock_path, log_path):
        for proc in psutil.process_iter():
            if proc.name in ('openerp', 'python', 'openerp-server'):
                lgn = proc.cmdline
                if ('--xmlrpc-port=%s' % build.port) in lgn:
                    try:
                        os.killpg(proc.pid, signal.SIGKILL)
                    except OSError:
                        pass
    def createdb_from_other(self, cr, uid, dbname, template, codification):
        self.pg_dropdb(cr, uid, dbname)
        _logger.debug("createdb from other %s", dbname)
        if can_use_db_template(cr, template):
            _logger.debug("I can use the template {0}".format(template))
            if not codification:
                codification = 'UTF-8'
            cmd = ['createdb',
                 '--encoding={0}'.format(codification),
                 '--template={0}'.format(template),
                 dbname]
        else:
            self.pg_createdb(cr, uid, dbname)
            cmd = "pg_dump {0} | psql {1}".format(template, dbname)
            _logger.debug("I cannot use the template {0}".format(template))
        _logger.debug(cmd)
        _logger.debug("End createdb from other")
        return cmd


    def job_25_restore(self, cr, uid, build, lock_path, log_path):
        build._log('job_25_restore', 'Duplicate template db %s' % build.dest)
        if not build.repo_id.db_name:
            return 0
        db_name = "%s-all" % build.dest
        if not build.repo_id.db_name_template:
            self.pg_createdb(cr, uid, db_name)
            cmd = "pg_dump %s | psql %s-all" % (build.repo_id.db_name, build.dest)
            return self.spawn(cmd, lock_path, log_path, cpu_limit=None, shell=True)
        else:
            cmd = self.createdb_from_other(cr, uid, db_name, build.repo_id.db_name, build.repo_id.db_codification)
            return self.spawn(cmd, lock_path, log_path, cpu_limit=None, shell=True)

    def job_26_upgrade(self, cr, uid, build, lock_path, log_path):
        build._log('job_26_upgrade', 'Update template db %s' % build.dest)
        if not build.repo_id.db_name:
            return 0
        cmd, mods = build.cmd()
        command = []
        if build.repo_id.update_modules:
            command = ['-u', build.repo_id.update_modules]
        cmd += ['-d', '%s-all' % build.dest]
        cmd += command
        cmd += ['--stop-after-init']
        return self.spawn(cmd, lock_path, log_path, cpu_limit=None)

    def job_27_restore(self, cr, uid, build, lock_path, log_path):
        build._log('job_27_restore', 'Create testing db %s' % build.dest)
        if not build.repo_id.db_name:
            return 0
        db_name = "%s-testing" % build.dest
        if not build.repo_id.use_testing_template:
            self.pg_createdb(cr, uid, db_name)
            cmd = "pg_dump %s | psql %s" % (build.repo_id.db_name_template_testing, db_name)
            
        else:
            cmd = self.createdb_from_other(cr, uid, db_name, build.repo_id.db_name_testing, build.repo_id.db_codification)
        build._log(cmd)
        return self.spawn(cmd, lock_path, log_path, cpu_limit=None, shell=True)

    def job_28_install_and_test(self, cr, uid, build, lock_path, log_path):
        build._log('job_28_install_and_test', 'Start installing modules testing db %s' % build.dest)
        if not build.repo_id.db_name:
            return 0
        cmd, mods = build.cmd()
        command = []
        if build.repo_id.test_update_modules:
            command = ['-i', build.repo_id.test_update_modules]
        cmd += ['-d', '%s-testing' % build.dest]
        cmd += command
        cmd += ['--stop-after-init', '--log-level=debug', '--test-enable']
        return self.spawn(cmd, lock_path, log_path, cpu_limit=None)

    def job_30_run(self, cr, uid, build, lock_path, log_path):
        if build.repo_id.db_name and build.state == 'running' and build.result == "ko":
            return 0
        runbot._re_error = self._get_regexeforlog(build=build, errlevel='error')
        runbot._re_warning = self._get_regexeforlog(build=build, errlevel='warning')

        build._log('run', 'Start running build %s' % build.dest)

        v = {}
        result = "ok"
        log_names = [elmt.name for elmt in build.repo_id.parse_job_ids]
        for log_name in log_names:
            log_all = build.path('logs', log_name+'.txt')
            if grep(log_all, ".modules.loading: Modules loaded."):
                if rfind(log_all, _re_error):
                    result = "ko"
                    break;
                elif rfind(log_all, _re_warning):
                    result = "warn"
                elif not grep(build.server("test/common.py"), "post_install") or grep(log_all, "Initiating shutdown."):
                    if result != "warn":
                        result = "ok"
            else:
                result = "ko"
                break;
            log_time = time.localtime(os.path.getmtime(log_all))
            v['job_end'] = time.strftime(openerp.tools.DEFAULT_SERVER_DATETIME_FORMAT, log_time)
        v['result'] = result
        build.write(v)
        build.github_status()

        # run server
        cmd, mods = build.cmd()
        if os.path.exists(build.server('addons/im_livechat')):
            cmd += ["--workers", "2"]
            cmd += ["--longpolling-port", "%d" % (build.port + 1)]
            cmd += ["--max-cron-threads", "1"]
        else:
            # not sure, to avoid old server to check other dbs
            cmd += ["--max-cron-threads", "0"]

        cmd += ['-d', "%s-all" % build.dest]

        if grep(build.server("tools/config.py"), "db-filter"):
            if build.repo_id.nginx:
                cmd += ['--db-filter','%d.*$']
            else:
                cmd += ['--db-filter','%s.*$' % build.dest]

        return self.spawn(cmd, lock_path, log_path, cpu_limit=None)

    def get_closest_branch_name(self, cr, uid, ids, target_repo_id, hint_branches, context=None):
        """Return the name of the odoo branch
        """
        for build in self.browse(cr, uid, ids, context=context):
            name = build.branch_id.branch_name
            if name.split('-',1)[0] == "saas":
                name = "%s-%s" % (name.split('-',1)[0], name.split('-',2)[1])
            else:
                name = name.split('-',1)[0]
            #retrieve last commit id for this branch
            build_ids = self.search(cr, uid, [('repo_id', '=', target_repo_id), ('branch_id.branch_name', '=', name)])
            if build_ids:
                thebuild = self.browse(cr, uid, build_ids, context=context)
                if thebuild:
                    return thebuild[0].name
            return name

    def _get_regexeforlog(self, build, errlevel):
        addederror = False
        regex = r'\d{4}-\d\d-\d\d \d\d:\d\d:\d\d,\d{3} \d+ '
        if build.repo_id.error == errlevel:
            if addederror:
                regex += "|"
            else:
                addederror = True
            regex +="(ERROR)"
        if build.repo_id.critical == errlevel:
            if addederror:
                regex += "|"
            else:
                addederror = True
            regex +="(CRITICAL)"
        if build.repo_id.warning == errlevel:
            if addederror:
                regex += "|"
            else:
                addederror = True
            regex +="(WARNING)"
        if build.repo_id.failed == errlevel:
            if addederror:
                regex += "|"
            else:
                addederror = True
            regex +="(TEST.*FAIL)"
        if build.repo_id.traceback == errlevel:
            if addederror:
                regex = '(Traceback \(most recent call last\))|(%s)' % regex
            else:
                regex = '(Traceback \(most recent call last\))'
        #regex = '^' + regex + '$'
        return regex

    def schedule(self, cr, uid, ids, context=None):
        """
        /!\ must rewrite the all method because for each build we need
            to remove jobs that were specified as skipped in the repo.
        """
        all_jobs = self.list_jobs()
        icp = self.pool['ir.config_parameter']
        timeout = int(icp.get_param(cr, uid, 'runbot.timeout', default=1800))

        for build in self.browse(cr, uid, ids, context=context):
            #remove skipped jobs
            jobs = all_jobs[:]
            for job_to_skip in build.repo_id.skip_job_ids:
                jobs.remove(job_to_skip.name)
            if build.state == 'pending':
                # allocate port and schedule first job
                port = self.find_port(cr, uid)
                values = {
                    'host': fqdn(),
                    'host_port': config['xmlrpc_port'],
                    'port': port,
                    'state': 'testing',
                    'job': jobs[0],
                    'job_start': now(),
                    'job_end': False,
                }
                build.write(values)
                cr.commit()
            else:
                # check if current job is finished
                lock_path = build.path('logs', '%s.lock' % build.job)
                if locked(lock_path):
                    # kill if overpassed
                    if build.job != jobs[-1] and build.job_time > timeout:
                        build.logger('%s time exceded (%ss)', build.job, build.job_time)
                        build.kill(result='killed')
                    continue
                build.logger('%s finished', build.job)
                # schedule
                v = {}
                # testing -> running
                if build.job == jobs[-2]:
                    v['state'] = 'running'
                    v['job'] = jobs[-1]
                    v['job_end'] = now(),
                # running -> done
                elif build.job == jobs[-1]:
                    v['state'] = 'done'
                    v['job'] = ''
                # testing
                else:
                    v['job'] = jobs[jobs.index(build.job) + 1]
                build.write(v)
            build.refresh()

            # run job
            if build.state != 'done':
                build.logger('running %s', build.job)
                job_method = getattr(self,build.job)
                lock_path = build.path('logs', '%s.lock' % build.job)
                log_path = build.path('logs', '%s.txt' % build.job)
                pid = job_method(cr, uid, build, lock_path, log_path)
                build.write({'pid': pid})
            # needed to prevent losing pids if multiple jobs are started and one them raise an exception
            cr.commit()

            # cleanup only needed if it was not killed
            if build.state == 'done':
                build.cleanup()

class job(osv.Model):
    _name = "runbot.job"

    _columns = {
        'name': fields.char("Job name")
    }

class runbot_repo(osv.Model):
    _inherit = "runbot.repo"

    def __init__(self, pool, cr):
        """
        /!\ the runbot_build class needs to be declared before the
            runbot_repo in the python file so that the list of jobs is complete.
        """
        build_obj = pool.get('runbot.build')
        jobs = build_obj.list_jobs()
        for job_name in jobs:
            query = "select id from runbot_job where name = '{0}'".format(job_name)
            cr.execute(query)
            job_id = [x[0] for x in cr.fetchall()]
            if not job_id:
                query = """insert into runbot_job (name) values ('{0}')""".format(job_name)
                cr.execute(query)
        query = "select id,name from runbot_job "
        cr.execute(query)
        job_to_rm_ids = [x[0] for x in cr.fetchall() if x[1] not in jobs]
        for job_id in job_to_rm_ids:
                query = "delete from runbot_job where id = {0}".format(job_id)
                cr.execute(query)
        return super(runbot_repo, self).__init__(pool, cr)

    _columns = {
        'db_name': fields.char("Database name to replicate"),
        'db_name_template': fields.boolean('Use DB Template?'),
        'db_codification': fields.char('Codification'),
        'update_modules': fields.char('Update these modules'),
        'use_testing_template': fields.boolean('Use DB Template for testing?'),
        'db_name_testing': fields.char('Db Name Template Testing'),
        'test_update_modules': fields.char('Install and test these modules',
                                           help="These modules should not be installed in Db Name Template Testing"),
        'nobuild': fields.boolean('Do not build'),
        'sequence': fields.integer('Sequence of display', select=True),
        'error': fields.selection(loglevels, 'Error messages'),
        'critical': fields.selection(loglevels, 'Critical messages'),
        'traceback': fields.selection(loglevels, 'Traceback messages'),
        'warning': fields.selection(loglevels, 'Warning messages'),
        'failed': fields.selection(loglevels, 'Failed messages'),
        'skip_job_ids': fields.many2many('runbot.job', string='Jobs to skip'),
        'parse_job_ids': fields.many2many('runbot.job', "repo_parse_job_rel", string='Jobs to parse'),
    }

    _defaults = {
        'error': 'error',
        'critical': 'error',
        'traceback': 'error',
        'warning': 'warning',
        'failed': 'none',
        'db_name_template': True,
        'use_testing_template': True,
        'db_codification': 'UTF-8',
        'update_modules': ''
    }

    _order = 'sequence'

    def update_git(self, cr, uid, repo, context=None):
        super(runbot_repo, self).update_git(cr, uid, repo, context)
        if repo.nobuild:
            bds = self.pool['runbot.build']
            bds_ids = bds.search(cr, uid, [('repo_id', '=', repo.id), ('state', '=', 'pending')], context=context)
            bds.write(cr, uid, bds_ids, {'state': 'done'}, context=context)

class RunbotControllerPS(runbot.RunbotController):

    def build_info(self, build):
        res = super(RunbotControllerPS, self).build_info(build)
        res['parse_job_ids'] = [elmt.name for elmt in build.repo_id.parse_job_ids]
        return res
