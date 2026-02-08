from setuptools import setup, find_packages

# Read requirements.txt for the install_requires field
with open('requirements.txt') as f:
    requirements = f.read().splitlines()

setup(
    name='jam_player',
    version='2.0.0',
    author='Zach',
    author_email='zach@effortlesspresence.com',
    description='The desktop application that runs JAM Digital Signage menus on the JAM Player devices.',
    long_description=open('README.md').read(),
    long_description_content_type='text/markdown',
    package_dir={'': 'src'},  # Tells setuptools packages are under src
    packages=find_packages(where='src',),  # Find packages in src
    include_package_data=True,
    install_requires=requirements,
    classifiers=[
        'Programming Language :: Python :: 3',
        'License :: OSI Approved :: MIT License',
        'Operating System :: OS Independent',
    ],
    python_requires='>=3.9'
)

