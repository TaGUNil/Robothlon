ui_mainwindow.py: mainwindow.ui
	pyuic5 $< > $@

all: ui_mainwindow.py

clean:
	rm ui_mainwindow.py

.PHONY: all clean
