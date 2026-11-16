#!/bin/sh
pkill -f 'llamafile.exe --server' ; pkill -f 'kiwix-serve --address' ; pkill -f 'miniserve .*/www' ; echo "Offline Kit stopped."
