                        INCLUDE_DIRS "."

# Debug only: `PAGER_DEBUG_NO_LIGHT_SLEEP=1 idf.py reconfigure build` makes
# modes_run() never light-sleep, so RTS stays asserted and the USB serial log
# stays alive. Never ship such a build: it runs the battery down at the awake current.
if(DEFINED ENV{PAGER_DEBUG_NO_LIGHT_SLEEP})
    target_compile_definitions(${COMPONENT_LIB} PRIVATE PAGER_DEBUG_NO_LIGHT_SLEEP=1)
    message(WARNING "PAGER_DEBUG_NO_LIGHT_SLEEP is set: this build never light-sleeps")
endif()
