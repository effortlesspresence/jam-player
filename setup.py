from setuptools import setup, find_packages
from pathlib import Path

# Read requirements.txt for the install_requires field
# Using services_v2/requirements.txt as the single source of truth for pinned versions
requirements_path = Path(__file__).parent / 'src' / 'jam_player' / 'services_v2' / 'requirements.txt'
with open(requirements_path) as f:
    # Filter out comments and empty lines
    requirements = [
        line.strip() for line in f.read().splitlines()
        if line.strip() and not line.strip().startswith('#')
    ]

# Read README.md if it exists
readme_path = Path(__file__).parent / 'README.md'
long_description = readme_path.read_text() if readme_path.exists() else 'JAM Player digital signage application'

setup(
    name='jam_player',
    version='2.0.0',
    author='Zach',
    author_email='zach@effortlesspresence.com',
    description='The desktop application that runs JAM Digital Signage menus on the JAM Player devices.',
    long_description=long_description,
    long_description_content_type='text/markdown',
    package_dir={'': 'src'},  # Tells setuptools packages are under src
    packages=find_packages(where='src',),  # Find packages in src
    include_package_data=True,
    install_requires=requirements,
    scripts=[
        'scripts/jam-simulate-network',  # Network simulation tool for testing
    ],
    classifiers=[
        'Programming Language :: Python :: 3',
        'License :: OSI Approved :: MIT License',
        'Operating System :: OS Independent',
    ],
    python_requires='>=3.9'
)

