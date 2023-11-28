from setuptools import setup, find_packages

setup(name = 'drnuke-bean',
      version = '0.1',
      description = "Dr Nukes's beancount arsenal",
      package_dir={'': 'src'},
      packages=find_packages(where='src'),
      zip_safe = False)