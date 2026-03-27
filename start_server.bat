@echo off
py local_booking_server.py --site-file "generated-site.html"
if errorlevel 1 pause
