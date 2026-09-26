# Out-of-context implementation of one parser top, non-project mode.
#
#   vivado -mode batch -source vivado_ooc.tcl -tclargs <period_ns> [top] [part] [flow] [io]
#
# Reads every .sv next to this script (packages first), constrains the clock,
# and writes reports to ./out_<top>_<period>_<flow>[_io]/.
#
# flow "default": opt/place/route with default directives.
# flow "explore": Explore directives plus post-place and post-route phys_opt.
# io "io": also time the ports, as if a flop sat directly on each one (0 ns
#   input and output delay). Without it, paths from s_axis_* are not timed.

set here   [file dirname [file normalize [info script]]]
set period [expr {$argc > 0 ? [lindex $argv 0] : 4.0}]
set top    [expr {$argc > 1 ? [lindex $argv 1] : "parser_top_eth_ipv4_udp"}]
set part   [expr {$argc > 2 ? [lindex $argv 2] : "xc7a35tcpg236-1"}]
set flow   [expr {$argc > 3 ? [lindex $argv 3] : "default"}]
set io     [expr {$argc > 4 && [lindex $argv 4] eq "io"}]
set out    [file join $here "out_${top}_${period}_${flow}[expr {$io ? {_io} : {}}]"]
file mkdir $out

set pkgs [lsort [glob -directory $here *_pkg.sv pkg_*.sv]]
set srcs {}
foreach f [lsort [glob -directory $here *.sv]] {
  if {[lsearch -exact $pkgs $f] < 0} { lappend srcs $f }
}
read_verilog -sv [concat $pkgs $srcs]

set xdc [file join $out clk.xdc]
set fh [open $xdc w]
puts $fh "create_clock -period $period -name clk \[get_ports clk\]"
if {$io} {
  puts $fh "set_input_delay -clock clk 0 \[get_ports -filter {DIRECTION == IN && NAME != clk}\]"
  puts $fh "set_output_delay -clock clk 0 \[all_outputs\]"
}
close $fh
read_xdc -mode out_of_context $xdc

synth_design -mode out_of_context -top $top -part $part
report_utilization -file [file join $out util_synth.rpt]

if {$flow eq "explore"} {
  opt_design -directive Explore
  place_design -directive ExtraTimingOpt
  phys_opt_design -directive AggressiveExplore
  route_design -directive AggressiveExplore
  phys_opt_design -directive AggressiveExplore
} else {
  opt_design
  place_design
  route_design
}

report_timing_summary -max_paths 10 -file [file join $out timing_summary.rpt]
report_timing -max_paths 10 -sort_by group -input_pins -file [file join $out timing_paths.rpt]
report_utilization -file [file join $out util_routed.rpt]
report_design_analysis -logic_level_distribution -file [file join $out logic_levels.rpt]
write_checkpoint -force [file join $out routed.dcp]

# Worst path into each block, so a fix in one does not hide the next.
set fh [open [file join $out per_block.rpt] w]
foreach blk {u_align u_accum u_parse u_payload} {
  set p [lindex [get_timing_paths -max_paths 1 -setup -to [get_cells -hier -filter "NAME =~ $blk/*"]] 0]
  if {$p ne ""} {
    puts $fh [format "%-10s slack=%7.3f levels=%2d  %s -> %s" $blk \
      [get_property SLACK $p] [get_property LOGIC_LEVELS $p] \
      [get_property STARTPOINT_PIN $p] [get_property ENDPOINT_PIN $p]]
  }
}
if {$io} {
  set p [lindex [get_timing_paths -max_paths 1 -setup -from [get_ports -filter {DIRECTION == IN && NAME != clk}]] 0]
  puts $fh [format "%-10s slack=%7.3f levels=%2d  %s -> %s" "ports_in" \
    [get_property SLACK $p] [get_property LOGIC_LEVELS $p] \
    [get_property STARTPOINT_PIN $p] [get_property ENDPOINT_PIN $p]]
}
close $fh

set wns [get_property SLACK [get_timing_paths -max_paths 1 -setup]]
set p   [lindex [get_timing_paths -max_paths 1 -setup] 0]
puts "RESULT flow=$flow io=$io top=$top period=$period wns=$wns fmax_mhz=[format %.1f [expr {1000.0 / ($period - $wns)}]]"
puts "RESULT path_from=[get_property STARTPOINT_PIN $p] path_to=[get_property ENDPOINT_PIN $p] levels=[get_property LOGIC_LEVELS $p]"
puts "RESULT datapath_ns=[get_property DATAPATH_DELAY $p]"
