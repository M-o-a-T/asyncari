from setuptools import setup, find_packages

exec(open("trio_ari/_version.py", encoding="utf-8").read())

LONG_DESC = open("README.rst", encoding="utf-8").read()

setup(
    name="trio-ari",
    version=__version__,
    description="A Trio-ified adapter for the Asterisk ARI interface",
    url="https://github.com/M-o-a-T/trio-ari",
    long_description=open("README.rst").read(),
    author="Matthias Urlichs",
    author_email="matthias@urlichs.de",
    license="MIT -or- Apache License 2.0",
    packages=find_packages(),
    install_requires=[
        "trio",
        "trio-asyncio",
        "aioari",
    ],
    keywords=[
        "asterisk",
    ],
    python_requires=">=3.6",
    classifiers=[
        "License :: OSI Approved :: MIT License",
        "License :: OSI Approved :: Apache Software License",
        "Operating System :: POSIX :: Linux",
        "Operating System :: MacOS :: MacOS X",
        "Programming Language :: Python :: 3 :: Only",
        "Programming Language :: Python :: Implementation :: CPython",
        "Programming Language :: Python :: Implementation :: PyPy",
        "Framework :: Trio",
        "Topic :: Communications :: Telephony",
        "Development Status :: 3 - Alpha",
    ],
)
