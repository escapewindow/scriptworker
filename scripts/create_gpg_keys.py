#!/usr/bin/env python
"""Create gpg keys for scriptworker instances."""

import argparse
import gnupg
import logging
import os
import shutil
import scriptworker.gpg


log = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO, format="%(message)s")


class FullPaths(argparse.Action):
    """Expand user- and relative-paths."""

    def __call__(self, parser, namespace, values, option_string=None):
        """Override the Action __call__ method."""
        setattr(namespace, self.dest, os.path.abspath(os.path.expanduser(values)))


parser = argparse.ArgumentParser(description="GPG keypair creation and export script")
parser.add_argument('-b', '--gpgbin', nargs='?', help="GPG binary used", action=FullPaths,
                    default="/usr/local/bin/gpg2")
parser.add_argument('-o', '--gpghome', nargs='?', help="GPG home folder", action=FullPaths,
                    default=os.path.join(os.getcwd(), "gpg"))
parser.add_argument('-u', '--username', nargs='?', help="Email username", required=True)
parser.add_argument('-s', '--hosts', nargs='+', help="Host names to generate for", required=True)
parser.add_argument('-e', '--expires', nargs='?', help="Validity period for key", default="2y")
parser.add_argument('--clean', action='store_true', default=False)
args = parser.parse_args()

gpgbinary = args.gpgbin
gpghome = args.gpghome
user = args.username
hosts = args.hosts
duration = args.expires
cleanup = args.clean

log.info("Using gnupghome {}".format(gpghome))

gpg = gnupg.GPG(
    gnupghome=gpghome,
    keyring=os.path.join(gpghome, "pubring.gpg"),
    secret_keyring=os.path.join(gpghome, "secring.gpg"),
    gpgbinary=gpgbinary,
)

if cleanup:
    try:
        shutil.rmtree(gpghome)
    except FileNotFoundError:
        pass
    os.makedirs(gpghome)

for host in hosts:
    log.info("Creating key for {}@{}".format(user, host))
    name = host.split('.')[0]
    fingerprint = scriptworker.gpg.generate_key(
        gpg, name, '', user + '@{}'.format(host),
        expiration=duration
    )
    log.info("Writing to %s.{pub,sec}", name)
    for pvt in (True, False):
        key = scriptworker.gpg.export_key(gpg, fingerprint, private=pvt)
        filename = "{}@{}.pub".format(user, host)
        if pvt:
            filename = "{}.sec".format(host)
        with open(filename, "w") as fh:
            print(key, file=fh, end='')
