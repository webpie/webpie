import traceback, sys, time, signal, importlib, yaml, os, os.path
from pythreader import Task, TaskQueue, Primitive, synchronized, PyThread, LogFile
from webpie import Logged, Logger, HTTPServer, RequestProcessor
from multiprocessing import Process, Pipe

import re, socket

setproctitle = None

try:    from setproctitle import setproctitle
except: pass

subst = re.compile("(%\((\w+)\))")

def expand_str(text, vars):
    out = []
    i0 = 0
    for m in subst.finditer(text):
        name = m.group(2)
        i1 = m.end(1)
        if name in vars:
            out.append(text[i0:m.start(1)])
            out.append(str(vars[name]))
        else:
            out.append(text[i0:i1])
        i0 = i1
    out.append(text[i0:])
    return "".join(out)


def expand(item, vars={}):
    if isinstance(item, str):
        item = expand_str(item, vars)
    elif isinstance(item, dict):
        new_vars = {}
        new_vars.update(vars)

        # substitute top level strings only
        out = {k:expand_str(v, vars) for k, v in item.items() if isinstance(v, str)}

        # use this as the substitution dictionary
        new_vars.update(out)    
        out.update({k:expand(v, new_vars) for k, v in item.items()})
        item = out
    elif isinstance(item, list):
        item = [expand(x, vars) for x in item]
    return item
            
class RequestTask(RequestProcessor, Task):
    
    def __init__(self, wsgi_app, request, logger):
        #print("RequestTask.__init__: args:", wsgi_app, request, logger)
        Task.__init__(self, name=f"[RequestTask {request.Id}]")
        RequestProcessor.__init__(self, wsgi_app, request, logger)

class Service(Primitive, Logged):
    
    def __init__(self, config, logger=None):
        name = config["name"]
        Primitive.__init__(self, name=f"[app {name}]")        
        Logged.__init__(self, f"[app {name}]", logger, debug=True)
        self.configure(config)

    @synchronized
    def configure(self, config=None):
        config = config or self.Config
        self.Config = config

        reload_files = config.get("touch_reload", [])
        if isinstance(reload_files, str):
            reload_files = [reload_files]

        self.ReloadFileTimestamps = {path: self.mtime(path) for path in reload_files}

        self.Prefix = config.get("prefix", "/")
        self.ReplacePrefix = config.get("replace_prefix")
        self.Timeout = config.get("timeout", 10)

        saved_path = sys.path[:]
        saved_modules = set(sys.modules.keys())
        saved_environ = os.environ.copy()
        try:
            args = None
            fname = config["file"]
            g = {}

            extra_path = config.get("python_path")
            if extra_path is not None:
                if isinstance(extra_path, str):
                    extra_path = [extra_path]
                sys.path = extra_path + sys.path

            if "env" in config:
                os.environ.update(config["env"])
                
            exec(open(fname, "r").read(), g)
            if "create" in config:
                args = config.get("args")
                app = g[config["create"]](args)
            else:
                app = g[config.get("application", "application")]
            self.AppArgs = args
            self.WSGIApp = app

            max_workers = config.get("max_workers", 5)
            queue_capacity = config.get("queue_capacity", 10)
            self.RequestQueue = TaskQueue(max_workers, capacity = queue_capacity,
                delegate=self)
            self.log("(re)configured")

        finally:
            sys.path = saved_path
            extra_modules = set(sys.modules.keys()) - set(saved_modules)
            #print("loadApp: removing modules:", sorted(list(extra_modules)))
            for m in extra_modules:
                del sys.modules[m]
            for n in set(os.environ.keys()) - set(saved_environ.keys()):
                del os.environ[n]
            os.environ.update(saved_environ)
            
    def taskFailed(self, queue, task, exc_type, exc_value, tb):
        self.log_error("request failed:", "\n".join(traceback.format_exception(exc_type, exc_value, tb)))

    def accept(self, request):
        header = request.HTTPHeader
        uri = header.URI
        self.debug("accept: uri:", uri, " prefix:", self.Prefix)
        if uri.startswith(self.Prefix):
            uri = uri[len(self.Prefix):]
            if not uri.startswith("/"):     uri = "/" + uri
            if self.ReplacePrefix:
                uri = self.ReplacePrefix + uri
            header.replaceURI(uri)
            request.AppName = self.Name
            self.RequestQueue.addTask(RequestTask(self.WSGIApp, request, self.Logger))
            return True
        else:
            return False
            
    def mtime(self, path):
        try:    return os.path.getmtime(path)
        except: return None

    def reloadIfNeeded(self):
        for path, old_timestamp in self.ReloadFileTimestamps.items():
            mt = self.mtime(path)
            if mt is not None and mt != old_timestamp:
                ct = time.ctime(mt)
                self.log(f"file {path} was modified at {ct}")
                break
        else:
            return False
        self.configure()

class MPLogger(PyThread):
    
    def __init__(self, logger, queue_size=-1, debug=False, name=None):
        import multiprocessing
        PyThread.__init__(self, name=name, daemon=True)
        self.Logger = logger
        self.Queue = multiprocessing.Queue(queue_size)
        self.Debug = debug

    def run(self):
        #
        # master side
        #
        from queue import Empty
        while True:
            msg = self.Queue.get()
            self.Logger.write(msg)
    
    def log(self, who, *parts):
        #
        # subprocess side
        #
        if self.Logger is not None:
            msg = "%s: %s: %s" % (time.ctime(), who, " ".join([str(p) for p in parts]))
            self.Queue.put(msg)
        
    debug = log


class MultiServerSubprocess(Process, Logged):
    
    def __init__(self, port, sock, config_file, logger=None):
        Process.__init__(self)
        print("MultiServerSubprocess.__init__: logger:", logger)
        Logged.__init__(self, "MultiServerSubprocess", logger)
        self.Sock = sock
        self.Port = port
        self.Server = None
        self.ConnectionToMaster, self.ConnectionToSubprocess = Pipe()
        self.ConfigFile = config_file   # path
        self.ReconfiguredTime = 0
        self.Services = []
        self.MasterSide = True
        self.Stop = False

    def reconfigure(self):
        self.log("reconfigure...")
        self.ReconfiguredTime = os.path.getmtime(self.ConfigFile)
        self.Config = config = expand(yaml.load(open(self.ConfigFile, 'r'), Loader=yaml.SafeLoader))
        
        templates = config.get("templates", {})
        services = config.get("services", [])
        
        service_list = []
        for svc_cfg in services:
            if "template" in svc_cfg:
                template = templates.get(svc_cfg.get("template", "*"))
                if template is not None:
                    c = {}
                    c.update(template)
                    c.update(svc_cfg)
                    svc_cfg = expand(c)
                service_names = svc_cfg.get("names", [svc_cfg.get("name")])
                for name in service_names:
                    c = svc_cfg.copy()
                    c["service_name"] = name
                    apps.append(Service(expand(c), self.Logger))
            else:
                service_list.append(Service(expand(svc_cfg), self.Logger))
        service_names = ",".join(s.Name for s in service_list)
        if self.Server is None:
            self.Server = HTTPServer.from_config(self.Config, service_list, logger=self.Logger)
            self.log(f"server created with services: {service_names}")
        else:
            self.Server.reconfigureApps(service_list)
            self.log(f"server reconfigured with services: {service_names}")
        self.Services = service_list
        self.log("reconfigured")

    CheckConfigInterval = 5.0
        
    def run(self):
        if setproctitle is not None:
            setproctitle("multiserver/%s/subprocess" % (self.Port,))
        pid = os.getpid()
        self.LogName = f"MultiServerSubprocess({pid})"
        self.reconfigure()
        self.MasterSide = False
        self.Sock.settimeout(5.0)
        last_check_config = 0
        
        while not self.Stop:
            
            try:    csock, caddr = self.Sock.accept()
            except socket.timeout:
                pass
            else:
                self.Server.connection_accepted(csock, caddr)
            
            if self.ConnectionToMaster.poll(0):
                msg = self.ConnectionToMaster.recv()
                self.log("message from master:", msg)
                if msg == "stop":
                    self.Stop = True
                elif msg == "reconfigure":
                    self.reconfigure()

            if not self.Stop and time.time() > last_check_config + self.CheckConfigInterval:
                if os.path.getmtime(self.ConfigFile) > self.ReconfiguredTime:
                    self.reconfigure()
                else:
                    for svc in self.Services:
                        if isinstance(svc, Service):
                            svc.reloadIfNeeded()
            last_check_config = time.time()
            
    def stop(self):
        if self.MasterSide:
            self.ConnectionToSubprocess.send("stop")
        else:
            self.Stop = True
            
    def request_reconfigure(self):
        self.ConnectionToSubprocess.send("reconfigure")
        self.log("reconfigure requested")
            
class MPMultiServer(PyThread, Logged):
            
    def __init__(self, config_file, logger=None):
        PyThread.__init__(self)
        Logged.__init__(self, "[Multiserver]", logger, debug=True)
        self.ConfigFile = config_file
        self.Server = None
        self.Port = None
        self.ReconfiguredTime = 0
        self.Subprocesses = []
        self.Sock = None
        self.MPLogger = MPLogger(logger) if logger is not None else None
        self.MPLogger.start()
        self.reconfigure()
    
    def reconfigure(self):
        self.log("reconfigure...")
        self.ReconfiguredTime = os.path.getmtime(self.ConfigFile)
        self.Config = config = expand(yaml.load(open(self.ConfigFile, 'r'), Loader=yaml.SafeLoader))

        port = self.Config["port"]
        if self.Port is None:
            self.Port = port
            self.Sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self.Sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            self.Sock.bind(('', self.Port))
            self.Sock.listen(10)
        elif port != self.Port:
            print("Can not change port number")
            sys.exit(1)
        
        new_nprocesses = self.Config["processes"]
        if new_nprocesses > len(self.Subprocesses):
            for p in self.Subprocesses:
                p.request_reconfigure()
            for _ in range(new_nprocesses - len(self.Subprocesses)):
                p = MultiServerSubprocess(self.Port, self.Sock, self.ConfigFile, logger=self.MPLogger)
                p.start()
                self.Subprocesses.append(p)
                self.log("started new subprocess")
        elif new_nprocesses < len(self.Subprocesses):
            while new_nprocesses < len(self.Subprocesses):
                p = self.Subprocesses.pop()
                p.stop()
                self.log("stopped a subprocess")
            for p in self.Subprocesses:
                p.request_reconfigure()
        else:
            for p in self.Subprocesses:
                p.request_reconfigure()
        self.log("subprocesses running now:", len(self.Subprocesses))
        
    def run(self):
        if setproctitle is not None:
            setproctitle("multiserver/%s/master" % (self.Port,))
        while True:
            time.sleep(5)
            if os.path.getmtime(self.ConfigFile) > self.ReconfiguredTime:
                self.reconfigure()
        

Usage = """
multiserver <config.yaml>
"""

class   SignalHandler:

    def __init__(self, signum, receiver):
        self.Receiver = receiver
        signal.signal(signum, self)
        
    def __call__(self, signo, frame):
        try:    
            self.Receiver.reconfigure()
        except: 
            import traceback
            traceback.print_exc()
            
def main():
    if not sys.argv[1:] or sys.argv[1] in ("-?", "-h", "--help", "help"):
        print(Usage)
        sys.exit(2)
    config_file = sys.argv[1]
    config = expand(yaml.load(open(config_file, 'r'), Loader=yaml.SafeLoader))
    logger = None
    if "logger" in config:
        cfg = config["logger"]
        debug = cfg.get("debug", False)
        if cfg.get("enabled", True):
            logger = Logger(cfg.get("file", "-"), debug=debug)
    if "pid_file" in config:
        open(config["pid_file"], "w").write(str(os.getpid()))
    ms = MPMultiServer(config_file, logger)
    s = SignalHandler(signal.SIGHUP, ms)
    ms.start()
    ms.join()

if __name__ == "__main__":
    main()