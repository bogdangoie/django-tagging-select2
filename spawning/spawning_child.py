"""spawning_child.py
"""

from eventlet import api, coros, greenio, wsgi

import optparse, os, signal, socket, sys, time

from paste.deploy import loadwsgi

from spawning import reloader_dev

import simplejson


class ExitChild(Exception):
    pass


def read_pipe_and_die(the_pipe, server_coro):
    try:
        api.trampoline(the_pipe, read=True)
        os.read(the_pipe, 1)
    except socket.error:
        pass
    try:
        os.close(the_pipe)
    except socket.error:
        pass
    api.switch(server_coro, exc=ExitChild)


class ExecuteInThreadpool(object):
    def __init__(self, app):
        self.app = app

    def __call__(self, env, start_response, exc_info=None):
        from eventlet import tpool
        head = []
        body = []
        def _start_response(status, headers, exc_info=None):
            head[:] = status, headers, exc_info
            return body.append

        def get_list():
            return list(self.app(env, _start_response))

        result = tpool.execute(get_list)
        start_response(*head)
        return result


def deadman_timeout(signum, frame):
    print "(%s) !!! Deadman timer expired, killing self with extreme prejudice" % (
        os.getpid(), )
    os.kill(os.getpid(), signal.SIGKILL)


def serve_from_child(sock, config):
    processpool_workers = config.get('processpool_workers', 0)
    threads = config.get('threadpool_workers', 0)
    wsgi_application = api.named(config['app_factory'])(config)

    if processpool_workers:
        from spawning import processpool_parent
        wsgi_application = processpool_parent.ExecuteInProcessPool(config)
    elif threads > 1:
        print "(%s) using %s threads" % (os.getpid(), threads, )
        wsgi_application = ExecuteInThreadpool(wsgi_application)
    elif threads != 1:
        print "(%s) not using threads, installing eventlet cooperation monkeypatching" % (
            os.getpid(), )
        from eventlet import util
        util.wrap_socket_with_coroutine_socket()
        #util.wrap_pipes_with_coroutine_pipes()
        #util.wrap_threading_local_with_coro_local()

    host, port = sock.getsockname()

    access_log_file = config['access_log_file']
    if access_log_file is not None:
        access_log_file = open(access_log_file, 'a')

    server_event = coros.event()
    try:
        wsgi.server(
            sock, wsgi_application, log=access_log_file, server_event=server_event)
    except KeyboardInterrupt:
        pass
    except ExitChild:
        pass

    ## Set a deadman timer to violently kill the process if it doesn't die after
    ## some long timeout.
    signal.signal(signal.SIGALRM, deadman_timeout)
    signal.alarm(config['deadman_timeout'])

    ## Once we get here, we just need to handle outstanding sockets, not
    ## accept any new sockets, so we should close the server socket.
    sock.close()

    server = server_event.wait()

    last_outstanding = None
    while server.outstanding_requests:
        if last_outstanding != server.outstanding_requests:
            print "(%s) %s requests remaining, waiting... (timeout after %s)" % (
                os.getpid(), server.outstanding_requests, config['deadman_timeout'])
        last_outstanding = server.outstanding_requests
        api.sleep(0.1)

    print "(%s) *** Child exiting: all requests completed at %s" % (
        os.getpid(), time.asctime())


def main():
    parser = optparse.OptionParser()
    parser.add_option("-r", "--reload",
        action='store_true', dest='reload',
        help='If --reload is passed, reload the server any time '
        'a loaded module changes.')

    options, args = parser.parse_args()

    if len(args) != 5:
        print "Usage: %s controller_pid httpd_fd death_fd factory_qual factory_args" % (
            sys.argv[0], )
        sys.exit(1)

    controller_pid, httpd_fd, death_fd, factory_qual, factory_args = args
    controller_pid = int(controller_pid)
    config = api.named(factory_qual)(simplejson.loads(factory_args))

    ## Set up the reloader
    if options.reload:
        watch = config.get('watch', None)
        if watch:
            watching = ' and %s' % watch
        else:
            watching = ''
        print "(%s) reloader watching sys.modules%s" % (os.getpid(), watching)
        api.spawn(
            reloader_dev.watch_forever, [], controller_pid, 1, watch)

    ## The parent will catch sigint and tell us to shut down
    signal.signal(signal.SIGINT, signal.SIG_IGN)
    api.spawn(read_pipe_and_die, int(death_fd), api.getcurrent())

    ## Make the socket object from the fd given to us by the controller
    sock = greenio.GreenSocket(
        socket.fromfd(int(httpd_fd), socket.AF_INET, socket.SOCK_STREAM))

    serve_from_child(
        sock, config)


if __name__ == '__main__':
    main()
