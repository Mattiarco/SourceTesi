# GENERATO AUTOMATICAMENTE
MODULE   := MXFP4DotProduct
RTL_DIR  := rtl
SIM_DIR  := sim
OBJ_DIR  := obj_dir
VERILATOR ?= verilator

.PHONY: all rtl sim run clean

all: run

rtl:
	sbt "runMain mxfp4.Elaborate"

sim:
	$(VERILATOR) --cc --exe --build -Wall -Wno-fatal -Wno-DECLFILENAME \
	  --top-module $(MODULE) --Mdir $(OBJ_DIR) -o sim_$(MODULE) \
	  -CFLAGS "-I$(CURDIR)/$(SIM_DIR) -O2" \
	  $(RTL_DIR)/*.sv $(SIM_DIR)/tb_$(MODULE).cpp

run: sim
	./$(OBJ_DIR)/sim_$(MODULE)

clean:
	rm -rf $(OBJ_DIR) target project/target
