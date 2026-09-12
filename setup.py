#!/usr/bin/env python3
"""
Hermes Remote Gateway Plugin - Setup and Installation
"""
from setuptools import setup, find_packages
from pathlib import Path

this_directory = Path(__file__).parent
long_description = (this_directory / "SKILL.md").read_text(encoding="utf-8")
requirements = (this_directory / "requirements.txt").read_text().strip().split("\n")

setup(
    name="hermes-remote-gateway",
    version="0.1.0",
    author="DKTunnel",
    author_email="",
    description="CLI plugin to connect Hermes CLI to a remote Hermes gateway via WebSocket",
    long_description=long_description,
    long_description_content_type="text/markdown",
    url="https://github.com/NousResearch/hermes-agent",
    packages=find_packages(),
    classifiers=[
        "Development Status :: 3 - Alpha",
        "Intended Audience :: Developers",
        "License :: OSI Approved :: MIT License",
        "Programming Language :: Python :: 3",
        "Programming Language :: Python :: 3.10",
        "Programming Language :: Python :: 3.11",
        "Programming Language :: Python :: 3.12",
    ],
    python_requires=">=3.10",
    install_requires=requirements,
    entry_points={
        "console_scripts": [
            "hermes-remote = hermes_remote_gateway.__init__:main",
        ],
    },
    include_package_data=True,
    zip_safe=False,
)