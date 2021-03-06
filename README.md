# Askandget
QandA forum based on askbot engine

Description: Askget will work alone or with other django apps (with some limitations, please see below), Django 1.1.1 - 1.2.3(*), MySQL(**) and PostgresQL(recommended) (>=8.3).
        
        
        Features
        ========
        
        * standard Q&A functionalities including votes, reputation system, etc.
        * user levels: admin, moderator, regular, suspended, blocked
        * per-user inbox for responses & flagged items (for moderators)
        * email alerts - instant and delayed, optionally tag filtered
        * search by full text and a set of tags simultaneously
        * can import data from stackexchange database file
        
        Installation
        ============
        
        The general steps are:
        
        * install the code
        * if there is no database yet - create one
        * create a new or configure existing django site for askandget
        * create/update the database tables
        
        Methods to install code
        -----------------------
        
        * **pip install askandget**
        * **easy_install askandget**
        * **download .tar.gz** file from the bottom of this page, then run **python setup.py install**
        * clone code from the github **git clone git://github.com/Askandget/anskandget-devel.git**, and then **python setup.py develop**
        
        Create/configure django site
        ----------------------------
        
        Either run command **startforum** or merge contents of directory **askandget/setup_templates** in the source code into your project directory.
        
        
        Create/update database tables
        -----------------------------
        
        Back up your database if it is not blank, then two commands:
        
        * python manage.py syncdb
        * python manage.py migrate
        
        There are two apps to migrate - askbot and django_authopenid (a forked version of the original, included within askbot), so you can as well migrate them separately
        
       
        Background Information
        ======================
        askandget is based on CNPROG project by Mike Chen and Sailing Cai, project which was originally inspired by StackOverflow and Yahoo Answers.
        
        Footnotes
        =========
        (*) - If you want to install with django 1.2.x a dependency "Coffin-0.3" needs to be replaced with "Coffin-0.3.3" - this will be automated in the future versions of the setup script.
        
        (**) - With MySQL you have to use MyISAM data backend, because it's the only one that supports Full Text Search.
Keywords: forum,community,wiki,Q&A


