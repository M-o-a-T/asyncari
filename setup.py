from setuptools import setup, find_packages

LONG_DESC = open("README.rst", encoding="utf-8").read()

setup(
    name="asyncari",
    use_scm_version={
        "version_scheme": "guess-next-dev",
        "local_scheme": "dirty-tag"
    },
    description="A AnyIO-ified adapter for the Asterisk ARI interface",
    url="https://github.com/M-o-a-T/asyncari",
    long_description=open("README.rst").read(),
    author="Matthias Urlichs",
    author_email="matthias@urlichs.de",
    license="MIT -or- Apache License 2.0",
    packages=find_packages(),
    setup_requires=[
        "setuptools_scm",
    ],
    install_requires=[
        "trio >= 0.11",
        "anyio >= 1.0.0",
        "asyncswagger11",
        "asks",
        "attrs>=18",
        "asyncwebsockets",
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
        "Framework :: AsyncIO",
        "Framework :: Trio",
        "Topic :: Communications :: Telephony",
        "Development Status :: 3 - Alpha",
    ],
)
