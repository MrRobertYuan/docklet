import time, threading, random, string, os, traceback
import master.monitor
import subprocess
from functools import wraps

from utils.log import initlogging, logger
from utils import env

class BatchJob(object):
    def __init__(self, user, job_info):
        self.user = user
        self.raw_job_info = job_info
        self.job_id = None
        self.job_name = job_info['jobName']
        self.job_priority = int(job_info['jobPriority'])
        self.status = 'pending'
        self.create_time = time.strftime('%Y-%m-%d %H:%M:%S',time.localtime())
        self.lock = threading.Lock()
        self.tasks = {}
        self.dependency_out = {}
        self.tasks_cnt = {'pending':0, 'scheduling':0, 'running':0, 'error':0, 'failed':0, 'finished':0}
        #self.top_sort()

        #init self.tasks & self.dependency_out & self.tasks_cnt
        raw_tasks = self.raw_job_info["tasks"]
        self.tasks_cnt['pending'] = len(raw_tasks.keys())
        for task_idx in raw_tasks.keys():
            task_info = raw_tasks[task_idx]
            self.tasks[task_idx] = {}
            self.tasks[task_idx]['config'] = task_info
            self.tasks[task_idx]['status'] = 'pending'
            self.tasks[task_idx]['dependency'] = []
            dependency = task_info['dependency'].strip().replace(' ', '').split(',')
            if len(dependency) == 1 and dependency[0] == '':
                continue
            for d in dependency:
                if not d in raw_tasks.keys():
                    raise ValueError('task %s is not defined in the dependency of task %s' % (t, task_idx))
                self.tasks[task_idx]['dependency'].append(d)
                if not d in self.dependency_out.keys():
                    self.dependency_out[d] = []
                self.dependency_out[d].append(task_idx)

    def data_lock(f):
        @wraps(f)
        def new_f(self, *args, **kwargs):
            self.lock.acquire()
            try:
                result = f(self, *args, **kwargs)
            except Exception as err:
                self.lock.release()
                raise err
            self.lock.release()
            return result
        return new_f

    # return the tasks without dependencies
    @data_lock
    def get_tasks_no_dependency(self,update_status=False):
        ret_tasks = []
        for task_idx in self.tasks.keys():
            if (self.tasks[task_idx]['status'] = 'pending' and
                len(self.tasks[task_idx]['dependency']) == 0):
                if update_status:
                    self.tasks_cnt['pending'] -= 1
                    self.tasks_cnt['scheduling'] += 1
                    self.tasks[task_idx]['status'] = 'scheduling'
                task_name = self.user + '_' + self.job_id + '_' + task_idx
                ret_tasks.append([task_name, self.tasks[task_idx]['config']])
        return ret_tasks

    # update status of this job based
    def _update_job_status(self):
        allcnt = len(self.tasks.keys())
        if self.tasks_cnt['failed'] != 0:
            self.status = 'failed'
        elif self.tasks_cnt['running'] != 0:
            self.status = 'running'
        elif self.tasks_cnt['finished'] == allcnt:
            self.status = 'done'
        else:
            self.status = 'pending'

    # start run a task, update status
    @data_lock
    def update_task_running(self, task_idx):
        old_status = self.tasks[task_idx]['status'].split('(')[0]
        self.tasks_cnt[old_status] -= 1
        self.tasks[task_idx]['status'] = 'running'
        self.tasks_cnt['running'] += 1
        self._update_job_status()

    # a task has finished, update dependency and return tasks without dependencies
    @data_lock
    def finish_task(self, task_idx):
        if task_idx not in self.tasks.keys():
            logger.error('Task_idx %s not in job. user:%s job_name:%s job_id:%s'%(task_idx, self.user, self.job_name, self.job_id))
            return []
        old_status = self.tasks[task_idx]['status'].split('(')[0]
        self.tasks_cnt[old_status] -= 1
        self.tasks[task_idx]['status'] = 'finished'
        self.tasks_cnt['finished'] += 1
        self._update_job_status()
        if task_idx not in self.dependency_out.keys():
            return []
        ret_tasks = []
        for out_idx in self.dependency_out[task_idx]:
            self.tasks[out_idx]['dependency'].remove(task_idx)
            if (self.tasks[out_idx]['status'] == 'pending' and
                len(self.tasks[out_idx]['dependency']) == 0):
                self.tasks_cnt['pending'] -= 1
                self.tasks_cnt['scheduling'] += 1
                self.tasks[out_idx]['status'] = 'scheduling'
                task_name = self.user + '_' + self.job_id + '_' + out_idx
                ret_tasks.append([task_name, self.tasks[out_idx]['config']])
        return ret_tasks

    # update error status of task
    @data_lock
    def update_task_error(self, task_idx, tried_times, try_out=False):
        old_status = self.tasks[task_idx]['status'].split('(')[0]
        self.tasks_cnt[old_status] -= 1
        self.tasks[task_idx]['status'] = 'error(tried %d times)' % int(tried_times)
        if try_out:
            self.tasks_cnt['failed'] += 1
        else:
            self.tasks_cnt['error'] += 1
        self._update_job_status()

class JobMgr(threading.Thread):
    # load job information from etcd
    # initial a job queue and job schedueler
    def __init__(self, taskmgr):
        threading.Thread.__init__(self)
        self.job_queue = []
        self.job_map = {}
        self.taskmgr = taskmgr
        self.fspath = env.getenv('FS_PREFIX')

    def run(self):
        while True:
            self.job_scheduler()
            time.sleep(2)

    # user: username
    # job_data: a json string
    # user submit a new job, add this job to queue and database
    def add_job(self, user, job_info):
        try:
            job = BatchJob(user, job_info)
            job.job_id = self.gen_jobid()
            self.job_queue.append(job.job_id)
            self.job_map[job.job_id] = job
        except ValueError as err:
            logger.error(err)
            return [False, err.args[0]]
        except Exception as err:
            return [False, err.args[0]]
        finally:
            return [True, "add batch job success"]

    # user: username
    # list a user's all job
    def list_jobs(self,user):
        res = []
        for job_id in self.job_queue:
            job = self.job_map[job_id]
            logger.debug('job_id: %s, user: %s' % (job_id, job.user))
            if job.user == user:
                all_tasks = job.raw_job_info['tasks']
                tasks_instCount = {}
                for task in all_tasks.keys():
                    tasks_instCount[task] = int(all_tasks[task]['instCount'])
                res.append({
                    'job_name': job.job_name,
                    'job_id': job.job_id,
                    'status': job.status,
                    'create_time': job.create_time,
                    'tasks': list(all_tasks.keys()),
                    'tasks_instCount': tasks_instCount
                })
        return res

    # user: username
    # jobid: the id of job
    # get the information of a job, including the status, json description and other information
    # call get_task to get the task information
    def get_job(self, user, job_id):
        pass

    # check if a job exists
    def is_job_exist(self, job_id):
        return job_id in self.job_queue

    # generate a random job id
    def gen_jobid(self):
        job_id = ''.join(random.sample(string.ascii_letters + string.digits, 8))
        while self.is_job_exist(job_id):
            job_id = ''.join(random.sample(string.ascii_letters + string.digits, 8))
        return job_id

    # this is a thread to process a job
    def job_processor(self, job):
        task_name, task_info = job.get_task()
        if not task_info:
            return False
        else:
            task_priority = job.job_priority
            self.taskmgr.add_task(job.user, task_name, task_info, task_priority)
            return True

    # this is a thread to schedule the jobs
    def job_scheduler(self):
        # choose a job from queue, create a job processor for it
        for job_id in self.job_queue:
            job = self.job_map[job_id]
            if self.job_processor(job):
                job.status = 'running'
                break
            #else:
                #job.status = 'done'

    # a task has finished
    def report(self, task):
        pass

    def get_output(self, username, jobid, taskid, instid, issue):
        filename = username + "_" + jobid + "_" + taskid + "_" + instid + "_" + issue + ".txt"
        fpath = "%s/global/users/%s/data/batch_%s/%s" % (self.fspath,username,jobid,filename)
        logger.info("Get output from:%s" % fpath)
        try:
            ret = subprocess.run('tail -n 100 ' + fpath,stdout=subprocess.PIPE,stderr=subprocess.STDOUT, shell=True)
            if ret.returncode != 0:
                raise IOError(ret.stdout.decode(encoding="utf-8"))
        except Exception as err:
            logger.error(traceback.format_exc())
            return ""
        else:
            return ret.stdout.decode(encoding="utf-8")
