@echo off
set ANDROID_HOME=E:\Android\android-sdk
node .\node_modules\appium\index.js --log-level debug %*
pause