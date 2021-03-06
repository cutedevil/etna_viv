#!/usr/bin/python
'''
Generate code to weave various CSOs into GPU state.
'''
# Copyright (c) 2012-2013 Wladimir J. van der Laan
#
# Permission is hereby granted, free of charge, to any person obtaining a
# copy of this software and associated documentation files (the "Software"),
# to deal in the Software without restriction, including without limitation
# the rights to use, copy, modify, merge, publish, distribute, sub license,
# and/or sell copies of the Software, and to permit persons to whom the
# Software is furnished to do so, subject to the following conditions:
#
# The above copyright notice and this permission notice (including the
# next paragraph) shall be included in all copies or substantial portions
# of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
# FITNESS FOR A PARTICULAR PURPOSE AND NON-INFRINGEMENT. IN NO EVENT SHALL
# THE AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
# LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING
# FROM, OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER
# DEALINGS IN THE SOFTWARE.
from __future__ import print_function, division, unicode_literals
import argparse
from collections import defaultdict
import sys
# Parse rules-ng-ng format for state space
from etnaviv.util import rnndb_path
from etnaviv.parse_rng import parse_rng_file, format_path, BitSet, Domain, Stripe, Register, Array, BaseType

SRC_SPEC = 'e->'
VARNAMES = ['x','y','z','w']

def parse_args():
    parser = argparse.ArgumentParser(description='Generate state merge function')
    parser.add_argument('--input', metavar='INFILE', type=str, 
            help='State definition file',
            default='data/viv_gallium_state.txt')
    parser.add_argument('--rules-file', metavar='RULESFILE', type=str, 
            help='State map definition file (rules-ng-ng)',
            default=rnndb_path('state.xml'))

    return parser.parse_args()        

def rnn_lookup(state_map, field):
    '''
    Look up dot-separated path to register in rnn domain.
    Return every step along the path.
    '''
    field = field.split('.')
    base = state_map
    path = []
    for comp in field:
        path.append(base)
        base = base.contents_by_name[comp]
    path.append(base)
    return path

def rnn_strides(path):
    '''
    Go along path, find stripes / registers with length >1, do cartesian product and compute state offsets for each
    member. Returns a tuple (offset,strides) where strides is a list of (stride,length) tuples.
    '''
    strides = []
    offset = 0
    for comp in path:
        if isinstance(comp, Domain):
            pass
        elif isinstance(comp, (Stripe, Array, Register)):
            if comp.length != 1:
                strides.append((comp.stride, comp.length))
            offset += comp.offset
        else:
            raise ValueError
    return offset, strides

def main():
    args = parse_args()
    state_xml = parse_rng_file(args.rules_file)
    state_map = state_xml.lookup_domain('VIVS')
    out = sys.stdout
    # Read input file
    with open(args.input, 'r') as f:
        fields = None
        recordname = None
        data = []
        for line in f:
            line = line.rstrip()
            if not line or line.startswith('#'):
                continue
            if line.startswith('    '):
                line = line.strip().split()
                fields.append(line[0])
            else:
                if recordname is not None:
                    data.append([recordname, fields])
                fields = []
                recordname = line.strip()
        if recordname is not None:
            data.append([recordname, fields])
    # Preprocess input file, look up field names and sort by offset
    recs_by_offset = defaultdict(list)
    field_by_offset = {}
    for rec in data:
        rec_info = rec[0].split()
        for field in rec[1]:
            path = rnn_lookup(state_map, field)
            offset, strides = rnn_strides(path)
            #print(field, '%05x' % offset, strides)
            field_by_offset[offset] = (field, path, strides)
            recs_by_offset[offset].append(rec_info)
    # Emit output
    offsets = sorted(field_by_offset.keys())
    last_dirty_bits = None
    indent = 1
    for offset in offsets:
        (name, path, strides) = field_by_offset[offset]
        # strides is a list of (stride,length) tuples
        name = name.replace('.', '_')
        recs = recs_by_offset[offset]

        # build target state
        target_state = name
        target_state_sub = ', '.join(('{%i}' % (src_idx)) for src_idx,_ in enumerate(strides))
        if target_state_sub:
            target_state += '('+target_state_sub+')'

        # build target field reference
        target_field = name
        #   to sort destination state addresses in order, sort array indices by decreasing stride
        dest_strides = sorted([(idx,stride,length) for idx,(stride,length) in enumerate(strides)], key=lambda x:-x[1]) 
        for src_idx,stride,length in dest_strides:
            target_field += '[{%i}]' % (src_idx)

        fieldrefs = [] 
        dirty_bits = set()
        for rec in recs:
            source_field = name
            for idx,(stride,length) in enumerate(strides):
                iname = '{%i}' % idx
                if not iname in rec[1]: # if quantifier not already used in record name itself
                    source_field += '[' + iname + ']'
            fieldrefs.append('%s%s%s' % (SRC_SPEC,rec[1],source_field))
            dirty_bits.add(rec[2])
       
        if last_dirty_bits != dirty_bits:
            if last_dirty_bits is not None:
                indent -= 1
                out.write('    ' * indent)
                out.write('}\n')
            out.write('    ' * indent)
            out.write('if(dirty & (%s))\n' % (' | '.join(dirty_bits)))
            out.write('    ' * indent)
            out.write('{\n')
            indent += 1
        for src_idx,stride,length in dest_strides:
            out.write('    ' * indent)
            out.write('for(int {0}=0; {0}<{1}; ++{0})\n'.format(VARNAMES[src_idx], length))
            out.write('    ' * indent)
            out.write('{\n')
            indent += 1

        macro = 'EMIT_STATE'
        if isinstance(path[-1].type, BaseType) and path[-1].type.kind == 'fixedp':
            macro += '_FIXP'

        out.write('    ' * indent)
        out.write('/*%05X*/ %s(%s, %s, %s);\n' % (
            offset,
            macro,
            target_state.format(*VARNAMES), 
            target_field.format(*VARNAMES), 
            (' | '.join(fieldrefs)).format(*VARNAMES)))
        
        for src_idx,stride,length in dest_strides:
            indent -= 1
            out.write('    ' * indent)
            out.write('}\n')

        last_dirty_bits = dirty_bits
    if last_dirty_bits is not None:
        indent -= 1
        out.write('    ' * indent)
        out.write('}\n')


if __name__ == '__main__':
    main()

