from uuid import uuid4
from time import time
import functools

from M import M

def session(method):
    """
    set a session cookie, redirect if someone is up to hijinx
    """
    @functools.wraps(method)
    def wrapper(self, *args, **kwargs):
        #variable names are schizo
        if not self.get_cookie('_uuid'):
            uid = str( uuid4())
            self.set_cookie( '_uuid', uid)
            M.db.sessions.insert( {'sessid':uid, 'last':time() }, safe=True)
        else:
            uid = self.get_cookie( '_uuid')

        self.session = M.db.sessions.find_one({'sessid':uid})
        if not self.session:
            return self.redirect( '/' )
        self.session['last'] = time()
        M.db.sessions.save( self.session)
        return method(self, *args, **kwargs)

    return wrapper

import tornado
class SessionHandler(tornado.web.RequestHandler):
    orderobj = None
    def get_current_user(self):
        return self.get_secure_cookie("_uuid")

    @property
    def user(self): return self.get_current_user()

    def render(self, template, **kwargs):
        #add some global kwargs
        kwargs['session'] = self.session
        html = self.render_string( template, **kwargs)
        self.finish( html)

