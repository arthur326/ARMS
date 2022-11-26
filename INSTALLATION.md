# Installation
We assume the same hardware and configuration as ours in this guide--we used a Raspberry Pi 4B (4GB)
and the Icom 7100. If you have different needs and know what you are doing, adjust
these steps accordingly.

## Prepare Raspberry Pi
Install Raspberry Pi OS onto your Micro SD card. (Consult the official documentation.)
Use a version that fits your needs
(Desktop or Lite) and perform any initial configuration.

**Name your user "arms"** unless you want to tweak service and script files and commands to work with another user.

These instructions were tested with Debian version 11 (bullseye), and we recommend that you use this release. Though our
first ARMS instance ran on Debian version 10 (buster), this required compiling Hamlib 4.x manually.

## Update and Install Dependencies
Connect to the internet if you have not already done so. Then run the following commands.
```commandline
sudo apt-get update
sudo apt-get upgrade
sudo apt-get install pulseaudio libportaudio2 libatlas-base-dev python3 python3-pip python3-venv python3-soundfile multimon-ng libhamlib4 libhamlib-utils git
```

## Configure ARMS
Place the ARMS directory (the one containing main.py) from the project into the home folder of the user arms.
To do this by cloning the repository, run:
```commandline
cd ~
git clone https://github.com/arthur326/ARMS.git
```

Ensure that you have the necessary audio (WAV) files within the included ARMS/audio directory. You must have
all of the following files in the aforementioned audio directory:
* All files that belong to any paragraph in the configuration (PARAGRAPHS section within arms_config.toml).
* Within the repeater_name subdirectory, a file xy.wav for every channel being scanned (channel 6 through LAST_CHANNEL in the configuration). Note that the filename xy.wav is always 2-digit; include a leading zero as necessary.
* Within the operator_name subdirectory, a file xyz.wav for every _active_ operator id. An operator id is active when it is set to true in the OPERATORS section within arms_config.toml. Note that the filename is always 3-digit.
* The file `ARMS_boot_error.wav`.

Next, set up a Python virtual environment for ARMS.
```commandline
cd ~/ARMS
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
deactivate
```

## Set Up a Static Name for the Serial Interface to Be Used by Hamlib
I followed [this tutorial](https://www.freva.com/assign-fixed-usb-port-names-to-your-raspberry-pi/)--thank you to the author, Frederic Vanvolsem.

Make sure your radio is not connected to the pi. Reboot the pi:
```commandline
sudo reboot
```
Once booted, run the command
```commandline
dmesg | grep ttyUSB
```
to see any USB serial devices initially present (likely none). Afterwards, connect the (powered-on) radio to a USB port on the Pi and
run the above command again. Note the new devices which have appeared. In our case, the Icom 7100
provides two serial interfaces and they appear as
```
usb 1-1.3.1: cp210x converter now attached to ttyUSB0
usb 1-1.3.3: cp210x converter now attached to ttyUSB1
```
and we want the first of these. This is because, according to the manual for the radio,
>Two COM port numbers are assigned to the [USB] connector. One of them is “USB1,” used for cloning and CI-V
>operation. The other one is “USB2,” whose function is selected in “USB2 Function” item of the “Connectors” Set mode.

Run the following command, replacing ttyUSB0, if necessary, with the name of your interface according to the output of the preceding commands:
```commandline
udevadm info --name=/dev/ttyUSB0 --attribute-walk
```
We need an attribute from the resulting output which will uniquely identify the serial interface. The line in the output we used to this end was of the form
```
ATTRS{serial}=="IC-7100 XXXXXXXX A"
```
(For the second serial interface (i.e., the one not of use to us, identified by ttyUSB1 in our case), we found that the corresponding serial attribute ends with "B" instead of "A". If you are seeing something else, or to be careful, you may want to test which ttyUSB device you can actually connect Hamlib to manually.)

Note down the line of the form `ATTRS{serial}=="IC-7100 XXXXXXXX A"` and run
```commandline
sudo nano /etc/udev/rules.d/10-usb-serial.rules
```
Insert the following line into the file
```
SUBSYSTEM=="tty", ATTRS{serial}=="IC-7100 XXXXXXXX A", SYMLINK+="ttyUSB_ARMS_RADIO"
```
adjusting the `ATTRS{serial}=="IC-7100 XXXXXXXX A"` part to your attribute, and save the changes.
Now run
```commandline
sudo udevadm trigger
```
You should see the symbolic link ttyUSB_ARMS_RADIO if you run
```commandline
ls /dev/ttyUSB*
```

## Set up the ARMS service
Enter the services directory provided with ARMS:
```commandline
cd ~/ARMS/services
```
If you are using a different radio (but again, we have never tested ARMS with other radios), modify arms-rigctld.sh (and arms_config.toml in the ARMS directory) accordingly.
Then move the scripts and units to the appropriate locations with the following commands.
```commandline
sudo mv arms*.sh /usr/local/bin/
sudo mv arms*.service /etc/systemd/system/
```
Add the execute permission to the scripts:
```commandline
sudo chmod u+x /usr/local/bin/arms*
```
Reload systemd:
```commandline
sudo systemctl daemon-reload
```

Enable and start the rigctld service:
```commandline
sudo systemctl enable arms-rigctld
sudo systemctl start arms-rigctld
```

We will now configure PAM to allow arms to su into itself without a password. The reason for the insanity of this and of
the command within arms.sh is due to pulseaudio-related madness. Run
```commandline
sudo nano /etc/pam.d/su
```
and insert the following lines below `auth       sufficient pam_rootok.so` in the file:
```
auth       [success=ignore default=1] pam_succeed_if.so user = arms
auth       sufficient   pam_succeed_if.so use_uid user ingroup arms
```
Save your changes. Then add the user arms to the arms group:
```commandline
sudo usermod -aG arms arms
```

You will probably want to test whether you can run ARMS before enabling the arms service.
**Ensure that it is safe for your radio to be controlled and transmit before doing this.** To start ARMS manually, you can execute the
same script as the service will:
```commandline
/usr/local/bin/arms.sh
```
Once you have gotten this to work, you can exit out of it with Ctrl+C.

When you are confident that ARMS is configured correctly from the above test, enable the ARMS service and reboot the system:
```commandline
sudo systemctl enable arms
sudo reboot
```
**Warning:** arms.service is configured to continuously reboot your system if ARMS cannot be started after many attempts. This will happen every 10-20 minutes if there is a configuration problem.
The intent behind this is to recover if a USB device becomes unavailable for some reason (though we have fortunately not
observed such an occurrence).

To set up a scheduled, daily reboot of the Raspberry Pi, edit the crontab of the root user:
```commandline
sudo crontab -e
```
If prompted, select your editor. Then add the following line to the file and save it:
```
0 12 * * * /usr/local/bin/arms-reboot.sh
```
The above line reboots the system at noon. The arms-reboot.sh script checks for a flag in the ARMS directory
to prevent a reboot when ARMS is in an alert or testing procedure.