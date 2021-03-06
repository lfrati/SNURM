import argparse
import json
import os
import subprocess as sub
from datetime import datetime, timedelta
from enum import Enum
from http.server import BaseHTTPRequestHandler, HTTPServer, HTTPStatus
from queue import Queue
from threading import Lock, Thread
from time import time

DEBUG = os.getenv("DEBUG", None) is not None


class Server:
    def __init__(self, counter, history):
        self.queue = Queue()
        self.running = None
        self.proc = None
        self.q_lock = Lock()
        self.path = os.path.dirname(os.path.realpath(__file__))
        self.counter_file = os.path.join(self.path, counter)
        self.log_file = os.path.join(self.path, history)

        if not os.path.exists(self.counter_file):
            with open(self.counter_file, "w") as f:
                f.write("0\n")

    def get_id(self):
        with open(self.counter_file, "r+") as f:
            val = int(f.read())
            val += 1
            f.seek(0)
            f.write(str(val))
            return val


class State(Enum):
    QUEUED = 1 # in queue, not started yet
    RUNNING = 2
    ENDED = 3
    CANCELLED = 4 # removed from queue before starting
    KILLED = 5 # killed while running


class Job:
    def __init__(self, cmd, path, env):
        self.queued = time()
        self.env = env  # like "conda activate deep"
        self.cmd = cmd  # no need to split if shell=True in Popen
        self.state = State.QUEUED
        self.launch_time = datetime.now()
        self.path = path
        self.id = server.get_id()
        self.out_file = os.path.join(self.path, f"job-{self.id}.out")

        self.script = f"cd {self.path}\n"
        if self.env != "":
            # https://github.com/conda/conda/issues/9296#issuecomment-537085104
            self.script = (
                self.script
                + f". $CONDA_PREFIX/etc/profile.d/conda.sh && conda activate {self.env}\n"
            )
        self.script = self.script + self.cmd

    def run(self):

        with open(self.out_file, "a") as log:
            self.start = time()
            self.state = State.RUNNING
            if DEBUG:
                print("Launching", self)

            log.write(str(self))
            log.flush()

            if DEBUG:
                print("Script", self.script)
            server.proc = sub.Popen(self.script, stdout=log, stderr=log, shell=True)
            server.proc.wait()
            self.end = time()

            if self.state == State.KILLED:
                log.write("=== JOB KILLED ===")
                if DEBUG:
                    print("Killed", self)
            else:
                self.state = State.ENDED
                log.write("=== JOB ENDED ===")
                if DEBUG:
                    print("Finished", self)

            self.record()
            log.flush()

    def record(self):
        print("Logging ", self.info())
        with open(server.log_file, "a") as h:
            h.write(str(self))

    def __repr__(self):
        return json.dumps(self.info()) + "\n"

    def info(self):

        if self.state == State.RUNNING:
            now = time()
            elapsed = str(timedelta(seconds=now - self.start))

        elif self.state == State.ENDED or self.state == State.KILLED:
            elapsed = str(timedelta(seconds=self.end - self.start))

        else:  # QUEUED, CANCELLED
            elapsed = str(timedelta(seconds=0))

        return {
            "id": self.id,
            "cmd": self.cmd,
            "elapsed": elapsed,
            "creation": self.launch_time.strftime("%Y/%m/%d-%H:%M:%S"),
            "state": self.state.name,
            "env": self.env,
            "path": self.out_file,
        }


class MainLoop(Thread):
    def run(self):

        while True:
            job = server.queue.get()
            # DANGER ZONE job is not queued nor running
            server.q_lock.acquire()
            server.queue.task_done()
            if job.state != State.CANCELLED:
                server.running = job
                server.q_lock.release()

                job.run()

                server.q_lock.acquire()
                server.running = None
                server.q_lock.release()
            else:
                server.q_lock.release()
                if DEBUG:
                    print(f"Discarding job {job.id} '{job}'")


class MyRequestHandler(BaseHTTPRequestHandler):
    def do_POST(self):
        if self.headers["Content-type"] == "application/json":
            content_len = int(self.headers["Content-length"])
            msg_bytes = self.rfile.read(content_len)
            msg_str = msg_bytes.decode("utf-8")
            msg = json.loads(msg_str)
            try:
                action = msg["action"]
            except KeyError as e:
                reply = {"result": "", "error": f"Missing {e} field."}
                self.send(reply)
                return

            if action == "add":
                try:
                    cmd = msg["cmd"]
                    path = msg["path"]
                    env = msg["env"]
                except KeyError as e:
                    reply = {"result": "", "error": f"Missing {e} field."}
                    self.send(reply)
                    return
                else:
                    job = Job(cmd, path, env)
                    server.queue.put(job)
                    if DEBUG:
                        print("Added", str(job), "to queue.")
                        print(self.status())
                    reply = {"result": job.info(), "error": ""}

            elif action == "cancel":
                try:
                    to_cancel = int(msg["id"])
                except KeyError as e:
                    reply = {"result": "", "error": f"Missing {e} field."}
                    self.send(reply)
                    return
                except ValueError as e:
                    reply = {"result": "", "error": f"Wrong ID format. {e}"}
                else:
                    if DEBUG:
                        print(f"Canceling {to_cancel}")
                    server.q_lock.acquire()
                    cancelled = None
                    for job in server.queue.queue:
                        if job.id == to_cancel:
                            cancelled = job
                            job.state = State.CANCELLED
                    server.q_lock.release()
                    if cancelled is not None:
                        if DEBUG:
                            print(self.status())
                        reply = {
                            "result": f"{cancelled.info()}",
                            "error": "",
                        }
                    else:
                        reply = {"result": "", "error": "Job not found."}
            elif action == "cancel_all":
                count = 0
                cancelled = []
                server.q_lock.acquire()
                for job in server.queue.queue:
                    if job.state != State.CANCELLED:
                        job.state = State.CANCELLED
                        cancelled.append(job)
                        count += 1
                server.q_lock.release()
                reply = {"result": cancelled, "error": ""}

            elif action == "kill":
                if server.running is not None and server.proc is not None:
                    server.proc.kill()
                    server.running.state = State.KILLED
                    reply = {
                        "result": f"{server.running.info()}",
                        "error": "",
                    }
                    server.running = None
                    server.proc = None
                else:
                    reply = {"result": f"", "error": ""}

            else:
                reply = {"result": "", "error": "Unrecognized action"}

            self.send(reply)

    def do_GET(self):
        # Ignoring self.path
        jobs = []
        if server.running is not None:
            jobs.append(server.running.info())
        for job in server.queue.queue:
            jobs.append(job.info())
        reply = {"result": jobs, "error": ""}
        self.send(reply)

    def status(self):
        if server.queue.qsize() == 0:
            msg = "Queue: No jobs."
        else:
            msg = "Queue:\n" + "\n".join([str(job) for job in server.queue.queue])
        return msg

    def send(self, msg):

        msg_str = json.dumps(msg)
        msg_bytes = msg_str.encode("utf-8")

        self.send_response(HTTPStatus.OK)
        self.send_header("Content-type", "application/json")
        self.send_header("Content-length", len(msg_bytes))
        self.end_headers()
        self.wfile.write(msg_bytes)


if __name__ == "__main__":
    global args
    global server

    # TODO: "conda activate X" is messed up if the server is ran from a conda env.
    #       See https://github.com/conda/conda/issues/9296#issuecomment-537085104
    #       It has something to do with the env variables being inherited.
    #       As a fix I make sure the server is launched in the base conda env.
    #       Obviously it will break at some point, good luck.
    p = sub.Popen("echo $CONDA_PREFIX", stdout=sub.PIPE, shell=True)
    conda = p.stdout.read().decode("utf-8")
    assert "envs" not in conda, "Deactivate conda environment first."

    parser = argparse.ArgumentParser(description="SNURM server")
    parser.add_argument("--addr", default="localhost")
    parser.add_argument("--port", default=12345)
    parser.add_argument("--counter", default="counter.txt")
    parser.add_argument("--log", default="history.log")
    args = parser.parse_args()

    server = Server(args.counter, args.log)

    t = MainLoop()
    t.daemon = True
    t.start()

    try:
        print(f"Listening on {args.addr}:{args.port}")
        s = HTTPServer((args.addr, args.port), MyRequestHandler)
        s.serve_forever()
    except KeyboardInterrupt:
        print(" Shutting server down.")
        s.socket.close()
