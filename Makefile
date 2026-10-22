.PHONY: build package clean

build:
	poetry install 

package: 
	pyinstaller sys-tray.spec --clean --noconfirm

clean:
	rm -rf build dist
	rm -rf sys_tray/__pycache__