FILES = router.py router_config_sample.yaml
DST_DIR = $(BUILD_DIR)/bin

build: $(DST_DIR)
	cp $(FILES) $(DST_DIR)
	cp -R sample-apps $(DST_DIR)
	cd $(DST_DIR); chmod ugo+x $(FILES)
    
$(DST_DIR):
	mkdir -p $@

    
