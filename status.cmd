@echo off
cd /d "%~dp0"
py -3 -m nightwatch status %*
