from appium_android import AndroidAppiumClient
from time import sleep

options = {
    "platformName": "Android",
    "platformVersion": "12",
    "deviceName": "127.0.0.1:16384",
    "automationName": "UiAutomator2",
    # "appPackage": "com.tencent.mm",
    # "appActivity": ".ui.LauncherUI",
    # "app": r"F:\workplace\test\wechat.apk",
    "fullReset": False,
    "noReset": True,
    'webviewConnectTimeout': 20000,
    'newCommandTimeout': 6000,
    'autoAcceptAlerts': False
}
appium = AndroidAppiumClient('http://127.0.0.1:4723')
print(appium.init_connection(options))
PACKAGE_NAME = "com.tencent.mm"
LAUNCH_ACTIVITY = ".ui.LauncherUI"
# sleep(100)
# print(appium.start_session({'app': APP_IDENT} | desired_caps))
print(appium.start_session(PACKAGE_NAME,LAUNCH_ACTIVITY))
