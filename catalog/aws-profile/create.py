#!/usr/bin/env python3
"""The profile the repository expects and its role's region. The token the
sandbox asks for credentials with is sbx's: it issues and keeps it."""
import re

import sbx_provider as sbx


def create(_action):
    profile, region = sbx.option('profile', 'sbx'), sbx.env('AWS_PROFILE_REGION', '')
    if not re.fullmatch(r'[A-Za-z0-9_][A-Za-z0-9_.-]{0,63}', profile):
        raise sbx.ProviderError('AWS profile must use 1-64 letters, numbers, underscores, dots or hyphens')
    if not re.fullmatch(r'[a-z]{2}(?:-[a-z0-9]+)+-\d+', region):
        raise sbx.ProviderError('Connect an AWS role with a valid region first')
    sbx.write_outputs({'profile': profile, 'region': region})


if __name__ == '__main__':
    sbx.main(create, ('create',))
