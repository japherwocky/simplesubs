#!/usr/bin/env python

import os, datetime
from copy import copy

import tornado.httpserver
import tornado.ioloop
import tornado.options
import tornado.web
from tornado.web import HTTPError

import amazonproduct
from key import access, secret
amznapi = amazonproduct.API( access, secret, 'us')

from M import M

from sessions import SessionHandler, session

cats = set( ['shampoo', 'toilet paper', 'face wash', 'deodorant', 'soap', 'tampons', 'foo'] )


class SeedHandler(SessionHandler):
    @session
    def get(self ):
        #docs at http://docs.amazonwebservices.com/AWSEcommerceService/4-0/
        keywords = self.get_argument('keywords', 'toiletpaper')
        query = amznapi.item_search( 'HealthPersonalCare', Keywords=keywords, ResponseGroup="Images, OfferSummary, ItemAttributes") 
        datas = [ (i.ASIN, i.MediumImage.URL, i.OfferSummary.LowestNewPrice.FormattedPrice, i.ItemAttributes.Title) for i in query.Items.Item]

        self.render( 'seed.html', datas=datas)

    @session
    def post( self):
        asin = self.get_argument('asin')
        category = self.get_argument( 'category')

        query = amznapi.item_lookup( asin, ResponseGroup="Images, OfferSummary, ItemAttributes") 
        datas = [ (i.ASIN, i.MediumImage.URL, i.OfferSummary.LowestNewPrice.FormattedPrice, i.ItemAttributes.Title) for i in query.Items.Item]

        obj = datas[-1]
        productobj = {  'asin':          asin,
                        'category':     category,
                        'medium_url':   str(obj[1]), #one of these chokes Mongo on insert
                        'price':        str(obj[2]), 
                        'title':        str(obj[3]),
                    }

        M.db.products.insert( productobj )

        self.render( 'seed.html', datas=datas)

class CartHandler( SessionHandler):
    @session
    def get( self):
        cart = M.db.carts.find_one( {'session':self.session['sessid']}) or {}

        productids = [cart[k].keys()[0] for k in cart.keys() if k in cats] #TODO, check against a set() of categories
        productdata = M.db.products.find( {'asin': {'$in':productids}})
        products = {}
        for p in productdata:
            products[ p['asin']] = p

        self.render( 'cart.html', cart=cart, products=products, price=calcprice( self.session))

    @session
    def post( self):
        #who validates?
        asin = self.get_argument("asin")
        freq = self.get_argument( "freq", False)
        start = self.get_argument( "start", False)
        category = self.get_argument( "category")

        cart = M.db.carts.find_one( {'session':self.session['sessid']})
        if not cart:
            #garbage - the widgets shouldn't be on if they haven't chosen a product
            return self.write("SHOO")

        cart[category][asin] = (freq, start)

        M.db.carts.save( cart, safe=True)

        return self.write( calcprice(self.session))


class ProductHandler( SessionHandler):
    """
    ajax widget to choose products for a particular category
    """
    @session
    def get( self, category):
        #look up records per category
        products = M.db.products.find( {'category':category})
        self.render('productchooser.html', datas=products)

    @session
    def post( self, category):
        """
        make a generic cart
        """
        cart = M.db.carts.find_one( {'session':self.session['sessid']}) or self.mkcart( )
        cart[category] = { self.get_argument('asin'): (False, False) }
        M.db.carts.save( cart)

        return self.redirect( '/cart' )
        

    def mkcart( self):
        cart = {'session': self.session['sessid'] }

        return cart


def calcprice( session):
    cart = M.db.carts.find_one( {'session': session['sessid']} )

    if not cart: return 0

    def monthlysum(freqs):
        #so.. figure out next shipment
        if not freqs[0]: return 0
        month,day,year = [int(d) for d in freqs[1].split('/')]
        nextship = datetime.datetime( year, month, day)
        today = datetime.datetime.today()
        assert nextship.month >= today.month

        if nextship.month > today.month: return 0

        token = copy( nextship)
        delta = datetime.timedelta( int(freqs[0]))
        monthsum = 0
        while (token + delta).month == today.month:
            monthsum += 1
            token += delta
        
        return monthsum

    def cost(asin, count):
        product = M.db.products.find_one( {'asin':asin})
        cost=float( product['price'][1:])
        price = cost * count
        price *= 1.1 #markup
        price += 5 #shipping

        return price



    products = [cart[k] for k in cart.keys() if k in cats]

    total = 0
    for p in products:
        for k in p.keys():
            count = monthlysum(p[k])
            total += cost(k, count)

    return '%.2f'%total


class HelloHandler( SessionHandler):
    @session
    def get( self):
        return self.render( 'index.html')


class App( tornado.web.Application):
    def __init__(self):
        here = os.path.dirname(__file__)
        _settings = dict(
            cookie_secret='verysekret',
            login_url="/login",
            template_path= os.path.join( here, "templates"),
            static_path=os.path.join( here, "static"),
            xsrf_cookies= False,
            debug = True,
        )

    
        handlers = [
            (r"/?", HelloHandler),
            (r"/seed/?", SeedHandler),
            (r"/cart/?", CartHandler),
            (r"/?(.+)/choose/?", ProductHandler),
        ]

        tornado.web.Application.__init__(self, handlers, **_settings)
    

def main():
    import sys
    import os.path
    from tornado.options import define, options

    # mangle path so modules in sub-dirs can import from root project dir
    sys.path.append(os.path.dirname(__file__))

    define("port", default=8200, help="run on the given port", type=int)
    define("runtests", default=False, help="Run unit tests", type=bool)
    tornado.options.parse_command_line()

    if options.runtests:
        #put tests in /tests/__init__.py
        import tests, unittest
        sys.argv = ['webserv.py',] #unittest goes digging in argv
        unittest.main( 'tests')
        return


    http_server = tornado.httpserver.HTTPServer( App() )
    http_server.listen(options.port)
    tornado.ioloop.IOLoop.instance().start()


if __name__ == "__main__":
    main()

