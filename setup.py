from setuptools import setup

setup(
    name='ringmaster',
    version='0.5.0',
    description='Circus Tcl/Tk Control Panel',
    author='Rafael Viotti',
    author_email='rviotti@gmail.com',
    install_requires=['aiozmq'],
    url='https://github.com/viotti/ringmaster',
    download_url = 'https://github.com/viotti/ringmaster/tarball/0.5.0',
    py_modules=['ringmaster'],
    entry_points = {'console_scripts': ['ringmaster = ringmaster:main']}
)
