from setuptools import setup

setup(
    name='ringmaster',
    version='0.1.0',
    description='Circus Tcl/Tk Control Panel',
    author='Rafael Viotti',
    author_email='rviotti@gmail.com',
    install_requires=['aiozmq'],
    url='https://github.com/viotti/ringmaster',
    download_url = 'https://github.com/viotti/ringmaster/tarball/0.1.0',
    py_modules=['ringmaster']
)
