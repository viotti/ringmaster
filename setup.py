from setuptools import setup

setup(
    name='ringmaster',
    version='0.6.1',
    description='Circus Tcl/Tk Control Panel',
    author='Rafael Viotti',
    author_email='rviotti@gmail.com',
    install_requires=['aiozmq'],
    url='https://github.com/viotti/ringmaster',
    download_url = 'https://github.com/viotti/ringmaster/tarball/0.6.1',
    py_modules=['ringmaster'],
    entry_points = {'console_scripts': ['ringmaster = ringmaster:main']}
)
