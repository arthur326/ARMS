## Purpose
ARMS (Amateur Radio Monitoring System) is a system for the automatic monitoring of radio
channels for LPZ/LTZ (long-press-zero or long-tone-zero), which is used to request help in an emergency. ARMS scans
a list of user-configured channels for the presence of LPZ, and upon detection of LPZ, begins an alert
procedure that notifies operators on a predetermined alert channel.

The intent behind this is to let operators be available to respond to emergencies without being bothered by
other transmissions, especially at night while sleeping. The alert channel should be tone-guarded using CTCSS or DCS so that
only transmissions from ARMS will come through.

For more details, please see documentation folder.

The system was designed by Steve Fletcher and developed by Arthur Drobot.

## Installation
See INSTALLATION.md

## Acknowledgements
Arthur wishes to thank Steve Fletcher for designing ARMS and giving me the opportunity to work on it. In addition, Arthur thanks Daniel Fedorin [(@DanilaFe)](https://github.com/DanilaFe) for being available for general advice on
multiple occasions and Chris Wheeler [(@grintor)](https://github.com/grintor) for responding to a query about his lovely-logger package.

We thank the creators of the following open-source projects:
* Hamlib
* samplerate
* multimon-ng
* Python packages:
    * lovely-logger
    * sounddevice
    * samplerate
    * soundfile
    * toml.