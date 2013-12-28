import os
import wsgiref.handlers
import logging
import re

from google.appengine.api import xmpp
from google.appengine.api import urlfetch
from google.appengine.api import memcache
from google.appengine.api.urlfetch import DownloadError
from google.appengine.api.labs import taskqueue
from google.appengine.api.labs.taskqueue import Task

from google.appengine.ext import db
from google.appengine.ext import webapp
from google.appengine.ext.webapp.util import run_wsgi_app

import twilio
from data_model import RegisteredUser
from data_model import EventLog
from data_model import EventTracker
from data_model import SystemStatus
from data_model import CallHistory

from BeautifulSoup import BeautifulSoup, Tag

ACCOUNT_SID = "FIXME"
ACCOUNT_TOKEN = "FIXME"
API_VERSION = '2010-04-01'
CALLER_ID = 'FIXME'

class TestHandler(webapp.RequestHandler):
    def get(self):
        call = CallHistory()
        call.callSid = 'delete me'
        call.caller = 'delete me'
        call.put()

## end Testhandler


class MainHandler(webapp.RequestHandler):

  def post(self):
      
      # this handler is intended for admin use only
      # only accept calls from my own phone
      caller = self.request.get('From')
      if caller != CALLER_ID:
          logging.error('illegal caller %s with message %s' % (caller,self.request.get('Body')))
          return
      
      # validate it is in fact coming from twilio
      if ACCOUNT_SID == self.request.get('AccountSid'):
        logging.info("was confirmed to have come from Twilio (%s)." % caller)
      else:
        logging.info("was NOT VALID.  It might have been spoofed (%s)!" % caller)
        return
      
      # determine the command from the message
      body = self.request.get('Body')
      if body is None:
          logging.error('empty command!?')
          return
      
      command = body.split()
      logging.info('processing new command %s from message %s' % (command[0],body))

      if command[0].lower() == 'help':
          # setup the response SMS
          smsBody = "disable, enable, event <#>, get, add <name> <name> <number>"
      elif command[0].lower() == 'disable':
          setService(False)
          smsBody = "turned service off!"
      elif command[0].lower() == 'enable':
          setService(True)
          smsBody = "turned service on!"
      elif command[0].lower() == 'add':
          addUser(body)
          smsBody = "added athlete %s" % command[1]
      elif command[0].lower() == 'event':
          setEvent(command[1])
          smsBody = "set event to %s" % command[1]
      elif command[0].lower() == 'get':
          smsBody = "current event is %s" % memcache.get('eventNumber')
      else:
          smsBody = "error... unsupported command [%s]" % command[0]

      logging.debug("responding to query with %s" % smsBody)
      r = twilio.Response()
      r.append(twilio.Sms(smsBody))
      self.response.out.write(r)

  def get(self):
      self.post()
      
## end MainHandler()

#CRAWL_BASE_URL = "http://www.allcityswim.org/AllCity2009/SWSwim2009/results/FridayResults/event"
#CRAWL_BASE_URL = "http://www.allcityswim.org/AllCity2010/HFSwim2010/Thursday/THev"
#CRAWL_BASE_URL = "http://www.allcityswim.org/AllCity2010/HFSwim2010/Saturday/SAev"

#CRAWL_BASE_URL = "http://pages.cs.wisc.edu/~gtracy/THev"
CRAWL_BASE_URL = "http://allcity2011.parkcrestpool.org/media/SAev"

class ScanEventHandler(webapp.RequestHandler):
    def get(self):
        self.post()
        return
    
    def post(self):
        
        if serviceOn() == False:
            return
        
        #  which event are we on?
        eventString = memcache.get("eventNumber")
        if eventString is None:
            eventResult = db.GqlQuery("SELECT * FROM EventTracker").get()
            if eventResult is None:
                logging.error("IMPOSSIBLE! We don't know what event we're on!?!")
                return
            else:
                eventString = str(eventResult.event)

        event = int(eventString)
        url = CRAWL_BASE_URL + eventString + ".htm"
        logging.debug("getting ready to look at %s" % url)
        loop = 0
        done = False
        result = None
        while not done and loop < 2:
            try:
                result = urlfetch.fetch(url, headers = {'Cache-Control' : 'max-age=240'})
                done = True;
            except urlfetch.DownloadError:
                logging.error("Error loading page (%s)... sleeping" % loop)
                if result:
                    logging.debug("Error status: %s" % result.status_code)
                    logging.debug("Error header: %s" % result.headers)
                    logging.debug("Error content: %s" % result.content)
                time.sleep(6)
                loop = loop+1
           
        if result is None or result.status_code != 200:
            logging.error("Exiting early: error fetching URL: " + str(result.status_code))
            return 
        
        # continue only if the event has been scored...
        if result.content.find('Page not found or results not available') > 0:
            logging.info("looks like event %s hasn't been scored yet" % eventString)
            logging.info(result.content)
            return
        
        if event == 30:
            nextEvent = 33
        else: 
            nextEvent = event + 1
            
        # persist the new event number
        setEvent(nextEvent)
        memcache.set("eventNumber",str(nextEvent))
        logging.debug('bumping up the event number to %s' % str(nextEvent))

        # loop through all of the registered users and spawn a task to
        # scrape the event URL to find the athlete
        q = db.GqlQuery("SELECT * FROM RegisteredUser")
        results = q.fetch(500)
        for r in results:
            # create an event to log the event
            logging.info('adding new finder task for %s' % r.athlete)
            task = Task(url='/athletefindertask', params={'phone':r.phone,
                                                          'athlete':r.athlete,
                                                          'url':url,
                                                          'event':event,})
            task.add('athletefinder')

        # send out the text with the results
        textBody = 'just completed event %s' % event
        task = Task(url='/sendsmstask', params={'phone':CALLER_ID,
                                                'athlete':'admin',
                                                'event':'status',
                                                'text':textBody,})
        task.add('smssender')

            
## end ScanEventHandler

class NewMsgHandler(webapp.RequestHandler):
    def get(self):
        users = []
        q = db.GqlQuery("SELECT * FROM RegisteredUser")
        results = q.fetch(500)
        for r in results:
            if r not in users:
                users.append(r)
                # create an event to log the event
                logging.info('send a message to %s' % r.phone)
                msg = 'Notifications are on run today. But the scoring table keeps changing the format of the results. So I quite literally have no idea what will happen. :) Greg'
                task = Task(url='/sendsmstask', params={'phone':r.phone,
                                                    'athlete':'ignore',
                                                    'event':'ignore',
                                                    'text':msg,})
                task.add('smssender')

## end NewMsgHandler
class AthleteFinderHandler(webapp.RequestHandler):
    
    def post(self):
        if serviceOn() == False:
            return
        
        logging.debug('athlete finder for %s' % self.request.get('athlete'))
        url = self.request.get('url')
        loop = 0
        done = False
        result = None
        while not done and loop < 2:
            try:
                result = urlfetch.fetch(url)
                done = True;
            except urlfetch.DownloadError:
                logging.error("Error loading page (%s)... sleeping" % loop)
                if result:
                    logging.debug("Error status: %s" % result.status_code)
                    logging.debug("Error header: %s" % result.headers)
                    logging.debug("Error content: %s" % result.content)
                time.sleep(6)
                loop = loop+1
           
        if result is None or result.status_code != 200:
            logging.error("Exiting early: error fetching URL: " + result.status_code)
            return 
     
        hit = False
        event = self.request.get('event')
        athlete = self.request.get('athlete')
        athlete_format = athlete.split(',')
        athlete = athlete_format[0] + ',&nbsp;' + athlete_format[1].strip()
        logging.debug('... formatted search on %s' % athlete)
        content_lines = result.content.split('\n')
        index = 0
        for line in result.content.split('\n'):
            #logging.debug('searchine content line %s' % line)
            
            if line.find(athlete) > -1:
                logging.debug('** HIT ** : %s' % line)
                
                soup = BeautifulSoup(line)
                name = soup.findAll("span")[0].string
                name_format = name.split(',')
                name = name_format[1].strip().split('&nbsp;')[1] + " " + name_format[0]
                logging.debug('** NAME ** : %s' % name)
                
                soup = BeautifulSoup(content_lines[index+1])
                time = soup.findAll("span")[0].string
                logging.debug('** TIME ** : %s' % time)
                
                soup = BeautifulSoup(content_lines[index+4])
                rank = soup.findAll("span")[0].string
                
                #soup = BeautifulSoup(content_lines[index+2])
                #q = soup.findAll("span")[0].string
                #if q == 'q':
                #  soup = BeautifulSoup(content_lines[index+5])
                #else:
                #  soup = BeautifulSoup(content_lines[index+4])
                #rank = soup.findAll("span")[0].string
                logging.debug('** RANK ** : %s' % rank)
                
                hit = True
                
                break
            index += 1
            
            
        soup = BeautifulSoup(result.content)
        for td in soup.html.body.findAll('do not do this loop'):
            logging.debug(td)
            if td.font.__str__().find(athlete) > 0:
                # athlete found!
                logging.debug("found: %s" % td.contents)
                # now climb up the tag chain to find all result details
                row = td.parent
                #logging.debug("parent: %s" % row.contents)
                
                rank = row.contents[1].font.string
                name = row.contents[5].font.string
                time = row.contents[13].font.string
                
                hit = True
                break
        
        if hit == False:
            # parse line by line
            lines = result.content.splitlines()
            for l in lines:
                #logging.debug(l)
                if l.find(athlete) > 0:
                    logging.debug("found raw: %s" % l)
                    data = re.search('(\d+)\s+(\w+\s\w+)\s+\d+\s.*?\d.*?\s+(([0-9]+:|)[0-9][0-9]\.[0-9][0-9])',l)
                    if data is not None:
                        rank = data.group(1).strip()
                        name = data.group(2).strip()
                        time = data.group(3).strip()
                        hit = True
                        break
                    else:
                        data = re.search('(\d+)\s+(\w+,\s\w+)\s+\d+\s.*?\d.*?\s+(([0-9]+:|)[0-9][0-9]\.[0-9][0-9])',l)
                        if data is not None:
                            rank = data.group(1).strip()
                            name = data.group(2).strip()
                            time = data.group(3).strip()
                            hit = True
                            break
                        else:                           
                            logging.error("False Positive!!")
            
        if hit == True:
            textBody = name + " finished event " + event + " in " + time + ", ranked " + rank
            logging.info(textBody)
                
            # send out the text with the results
            task = Task(url='/sendsmstask', params={'phone':self.request.get('phone'),
                                                    'athlete':athlete,
                                                    'event':event,
                                                    'text':textBody,})
            task.add('smssender')

            # create an event to log the event
            task = Task(url='/loggingtask', params={'phone':self.request.get('phone'),
                                                    'athlete':self.request.get('athlete'),
                                                    'event':event,
                                                    'text':textBody,})
            task.add('eventlogger')
        #else:
        #    logging.info("unable to find athlete %s for event %s!" % (athlete,self.request.get('event')))
        
        return
    
## end AthleteFinderHandler

class LogEventHandler(webapp.RequestHandler):
    def post(self):
      # log this event...
      log = EventLog()
      log.phone = self.request.get('phone')
      log.body = self.request.get('text')
      log.athlete = self.request.get('athlete')
      log.event = self.request.get('event')
      log.put()
    
## end LogEventHandler

class SendStatusHandler(webapp.RequestHandler):
  def get(self):
    if serviceOn() == False:
       return

    q = db.GqlQuery("select * from EventLog")
    events = q.fetch(500)
    
    callers = dict()
    athletes = dict()
    for e in events:
        if e.phone in callers:
            callers[e.phone] += 1
        else:
            callers[e.phone] = 1
            
        if e.athlete in athletes:
            athletes[e.athlete] += 1
        else:
            athletes[e.athlete] = 1
    
    stats = []
    for key,value in athletes.items():
        stats.append({'athlete':key,
                      'counter':value,
                      })
        
    textBody = str(len(events)) + " total calls... " + str(len(callers)) + " callers for " + str(len(athletes)) + " athletes"
    
    account = twilio.Account(ACCOUNT_SID, ACCOUNT_TOKEN)
    sms = {
           'From' : CALLER_ID,
           'To' : CALLER_ID,
           'Body' : textBody,
           }
    try:
        logging.info("Status SMS sent to %s" % self.request.get('phone'))
        account.request('/%s/Accounts/%s/SMS/Messages' % (API_VERSION, ACCOUNT_SID),
                        'POST', sms)
    except Exception, e:
        logging.error("Twilio REST error: %s" % e)

    return
## end


# this handler is intended to send out SMS messages
# via Twilio's REST interface
class SendSMSHandler(webapp.RequestHandler):
    
    def post(self):
      #return
      logging.info("Outbound SMS for ID %s to %s" % 
                   (self.request.get('sid'), self.request.get('phone')))
      account = twilio.Account(ACCOUNT_SID, ACCOUNT_TOKEN)
      sms = {
             'From' : CALLER_ID,
             'To' : self.request.get('phone'),
             'Body' : self.request.get('text'),
             }
      try:
          logging.info("SMS sent to %s... %s" % (self.request.get('phone'),self.request.get('text')))
          account.request('/%s/Accounts/%s/SMS/Messages' % (API_VERSION, ACCOUNT_SID),
                          'POST', sms)
      except Exception, e:
          logging.error("Twilio REST error: %s" % e)
                        
## end SendSMSHandler


class AddSwimmerHandler(webapp.RequestHandler):
    def get(self, swimmer="", phone=""):
        entry = RegisteredUser()
        entry.athlete = swimmer
        entry.phone = phone
        #entry = EventTracker()
        #entry.event = 1
        entry.put()
        return

class EventTestHandler(webapp.RequestHandler):
    def get(self, event=""):
        #CRAWL_BASE_URL = "http://pages.cs.wisc.edu/~gtracy/THev"  #15.htm
        url = CRAWL_BASE_URL + event + ".htm"
        logging.info("getting ready to look at %s" % url)
        loop = 0
        done = False
        result = None
        while not done and loop < 2:
            try:
                result = urlfetch.fetch(url)
                done = True;
            except urlfetch.DownloadError:
                logging.error("Error loading page (%s)... sleeping" % loop)
                if result:
                    logging.debug("Error status: %s" % result.status_code)
                    logging.debug("Error header: %s" % result.headers)
                    logging.debug("Error content: %s" % result.content)
                time.sleep(6)
                loop = loop+1
           
        if result is None or result.status_code != 200:
            logging.error("Exiting early: error fetching URL: " + str(result.status_code))
            return 
        
        # continue only if the event has been scored...
        if result.content.find('not yet available') > 0:
            logging.info("looks like event %s hasn't been scored yet" % event)
            return
        
        # loop through all of the registered users and spawn a task to
        # scrape the event URL to find the athlete
        q = db.GqlQuery("SELECT * FROM RegisteredUser")
        results = q.fetch(500)
        for r in results:
            # create an event to log the event
            logging.debug("creating task for %s, event %s, calling %s" % (r.athlete,event,r.phone))
            task = Task(url='/athletefindertask', params={'phone':r.phone,
                                                          'athlete':r.athlete,
                                                          'url':url,
                                                          'event':event,})
            task.add('athletefinder')
        
        return

class SetServiceHandler(webapp.RequestHandler):
    def get(self,status=""):
        if status == 'on':
            setService(True)
        else:
            setService(False)
            
        return
## end

def serviceOn():
    status = memcache.get('appstatus')
    if status is None:
        result = db.GqlQuery("select * from SystemStatus").get()
        status = result.status
        memcache.set('appstatus',status)
        
    if status == 'off':
        logging.info("trying to run, but the kill switch is enabled!")
        return False
    
    return True
## end

def setService(status):
    result = db.GqlQuery("select * from SystemStatus").get()
    if result is None:
        result = SystemStatus()
        
    if status == True:
        memcache.set('appstatus','on')
        result.status = 'on'
        logging.info("service has been turned ON")
    else:
        memcache.set('appstatus','off')
        result.status = 'off'
        logging.info("service has been turned OFF")
        
    result.put()
    return
## end


class SetEventHandler(webapp.RequestHandler):
    def get(self,status=""):
      logging.debug('setting event via the web to %s' % status)
      setEvent(status)
## end      
      
def setEvent(event):
    memcache.set("eventNumber", event)
    eventResult = db.GqlQuery("SELECT * FROM EventTracker").get()
    if eventResult is None:
        eventResult = EventTracker()
    eventResult.event = int(event)
    eventResult.put()
    return
## end

# sms command : "add Tracy aaabbbcccc"
def addUser(body):
    commands = body.split()
    user = RegisteredUser()
    user.phone = commands[3]
    user.athlete = commands[2] + ", " + commands[1]
    user.put()
    logging.info("added new user, %s, for number %s" % (commands[1],commands[2]))
    return
## end

      
def main():
  logging.getLogger().setLevel(logging.DEBUG)
  application = webapp.WSGIApplication([('/request', MainHandler),
                                        ('/scanevents', ScanEventHandler),
                                        ('/eventtest/(.*)', EventTestHandler),
                                        ('/athletefindertask', AthleteFinderHandler),
                                        ('/sendsmstask', SendSMSHandler),
                                        ('/service/(.*)', SetServiceHandler),
                                        ('/setevent/(.*)', SetEventHandler),
                                        ('/loggingtask', LogEventHandler),
                                        ('/sendstatus', SendStatusHandler),
                                        ('/addswimmer/(.*)/(.*)', AddSwimmerHandler),
                                        ('/test',TestHandler),
                                        ('/newmessage',NewMsgHandler),
                                        ],
                                       debug=True)
  wsgiref.handlers.CGIHandler().run(application)


if __name__ == '__main__':
  main()
