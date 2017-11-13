#!/usr/bin/env python

from setuptools import setup

setup(name="stravaweblib",
      version="0.0.3",
      description="Extends the Strava v3 API using web scraping",
      url="https://github.com/pR0Ps/stravaweblib",
      license="MPLv2",
      classifiers=[
          "Development Status :: 3 - Alpha",
          "Intended Audience :: Developers",
          "License :: OSI Approved :: Mozilla Public License 2.0 (MPL 2.0)",
          "Operating System :: OS Independent",
          "Programming Language :: Python :: 3",
          "Programming Language :: Python :: 3 :: Only",
          "Programming Language :: Python :: 3.3",
          "Programming Language :: Python :: 3.4",
          "Programming Language :: Python :: 3.5",
          "Programming Language :: Python :: 3.6",
          "Topic :: Software Development :: Libraries :: Python Modules"
      ],
      packages=["stravaweblib"],
      install_requires=["stravalib>=0.6.6,<1.0.0", "html5lib<1.0.0",
                        "beautifulsoup4>=4.6.0,<5.0.0"]
)
