#!/usr/bin/env python
'''
Long read assembly checker

Author: Ryan Wick
email: rrwick@gmail.com
'''
from __future__ import print_function
from __future__ import division

import sys
import re
import random
import imp
import os
import string
import argparse
# import time
from multiprocessing import cpu_count
# from multiprocessing import Process, Manager


from semi_global_long_read_aligner import AlignmentScoringScheme, Read, Reference, load_references, \
                                          load_long_reads, quit_with_error, get_nice_header, \
                                          get_random_sequence_alignment_error_rates, \
                                          reverse_complement, int_to_str, float_to_str, \
                                          print_progress_line, check_file_exists

'''
VERBOSITY controls how much the script prints to the screen.
'''
VERBOSITY = 0

def main():
    '''
    Script execution starts here.
    '''
    args = get_arguments()
    
    check_file_exists(args.sam)
    check_file_exists(args.ref)
    check_file_exists(args.reads)
    
    if args.html:
        check_plotly_exists()

    references = load_references(args.ref, VERBOSITY)
    reference_dict = {x.name: x for x in references}
    read_dict, _ = load_long_reads(args.reads, VERBOSITY)
    scoring_scheme = get_scoring_scheme_from_sam(args.sam)
    alignments = load_sam_alignments(args.sam, read_dict, reference_dict, scoring_scheme,
                                     args.threads)

    count_depth_and_errors_per_base(references, reference_dict, alignments)
    count_depth_and_errors_per_window(references, args.window_size)

    high_error_rate, very_high_error_rate = determine_thresholds(scoring_scheme, references)

    if VERBOSITY > 0:
        produce_console_output(references)

    if args.window_tables:
        window_tables_prefix = prepare_output_dirs(args.window_tables)
        produce_window_tables(references, window_tables_prefix)

    if args.base_tables:
        base_tables_prefix = prepare_output_dirs(args.base_tables)
        produce_base_tables(references, base_tables_prefix)

    if args.html:
        html_prefix = prepare_output_dirs(args.html)
        produce_html_files(references, html_prefix, high_error_rate, very_high_error_rate)

    sys.exit(0)

def get_arguments():
    '''
    Specifies the command line arguments required by the script.
    '''
    parser = argparse.ArgumentParser(description='Long read assembly checker',
                                     formatter_class=argparse.ArgumentDefaultsHelpFormatter)

    parser.add_argument('--sam', type=str, required=True, default=argparse.SUPPRESS,
                        help='Input SAM file of alignments')
    parser.add_argument('--ref', type=str, required=True, default=argparse.SUPPRESS,
                        help='FASTA file containing one or more reference sequences')
    parser.add_argument('--reads', type=str, required=True, default=argparse.SUPPRESS,
                        help='FASTQ file of long reads')
    parser.add_argument('--window_size', type=int, required=False, default=100,
                        help='Window size for error summaries')
    parser.add_argument('--window_tables', type=str, required=False, default=argparse.SUPPRESS,
                        help='Path and/or prefix for table files summarising reference errors for '
                             'reference windows (default: do not save window tables)')
    parser.add_argument('--base_tables', type=str, required=False, default=argparse.SUPPRESS,
                        help='Path and/or prefix for table files summarising reference errors at '
                             'each base (default: do not save base tables)')
    parser.add_argument('--html', type=str, required=False, default=argparse.SUPPRESS,
                        help='Path and/or prefix for html files with plots (default: do not save '
                             'html files)')
    parser.add_argument('--threads', type=int, required=False, default=argparse.SUPPRESS,
                        help='Number of CPU threads used to align (default: the number of '
                             'available CPUs)')
    parser.add_argument('--verbosity', type=int, required=False, default=1,
                        help='Level of stdout information (0 to 2)')

    args = parser.parse_args()

    global VERBOSITY
    VERBOSITY = args.verbosity

    # If some arguments weren't set, set them to None/False. We don't use None/False as a default
    # in add_argument because it makes the help text look weird.
    try:
        args.window_tables
    except AttributeError:
        args.window_tables = None
    try:
        args.base_tables
    except AttributeError:
        args.base_tables = None
    try:
        args.html
    except AttributeError:
        args.html = None
    try:
        args.threads
    except AttributeError:
        args.threads = cpu_count()
        if VERBOSITY > 2:
            print('\nThread count set to', args.threads)

    return args

def prepare_output_dirs(output_prefix):
    '''
    Ensures the output prefix is nicely formatted and any necessary directories are made.
    '''
    if output_prefix is None:
        return None
    if os.path.isdir(output_prefix) and not output_prefix.endswith('/'):
        output_prefix += '/'
    if output_prefix.endswith('/') and not os.path.isdir(output_prefix):
        os.makedirs(output_prefix)
    if not output_prefix.endswith('/'):
        directory = os.path.dirname(output_prefix)
        if directory and not os.path.isdir(directory):
            os.makedirs(directory)
    return output_prefix

def check_plotly_exists():
    '''
    Checks to see if the plotly library is available. If so, it's imported. If not, quit with an
    error.
    '''
    try:
        imp.find_module('plotly')
    except ImportError:
        quit_with_error('plotly not found - please install plotly package to produce html plots')

def get_scoring_scheme_from_sam(sam_filename):
    '''
    Looks for the 'SC' tag in the SAM file to get the alignment scoring scheme.
    '''
    sam_file = open(sam_filename, 'r')
    for line in sam_file:
        line = line.strip()

        # If we've reached the end of the header and still not found the scoring scheme, just
        # return a simple generic one.
        if not line.startswith('@'):
            return AlignmentScoringScheme('1,-1,-1,-1')

        line_parts = line.split('\t')
        for part in line_parts:
            if part.startswith('SC:'):
                scoring_scheme_string = part[3:]
                if scoring_scheme_string.count(',') == 3:
                    return AlignmentScoringScheme(scoring_scheme_string)

    return AlignmentScoringScheme('1,-1,-1,-1')

def get_random_sequence_error_rate(scoring_scheme):
    '''
    Returns the expected number of errors per reference base for an alignment of random sequences
    using the given scoring scheme.
    '''
    # I've precalculated the error rate for some typical scoring schemes.
    scoring_scheme_str = str(scoring_scheme)
    if scoring_scheme_str == '1,0,0,0':
        return 0.587
    elif scoring_scheme_str == '0,-1,-1,-1':
        return 0.526
    elif scoring_scheme_str == '1,-1,-1,-1':
        return 0.533
    elif scoring_scheme_str == '5,-4,-8,-6':
        return 0.527
    elif scoring_scheme_str == '5,-6,-10,0':
        return 1.012
    elif scoring_scheme_str == '2,-5,-2,-1':
        return 0.713
    elif scoring_scheme_str == '1,-3,-5,-2':
        return 0.544
    elif scoring_scheme_str == '5,-11,-2,-4':
        return 0.707
    elif scoring_scheme_str == '3,-6,-5,-2':
        return 0.641
    elif scoring_scheme_str == '2,-3,-5,-2':
        return 0.546
    elif scoring_scheme_str == '1,-2,0,0':
        return 0.707
    elif scoring_scheme_str == '0,-6,-5,-3':
        return 0.575
    elif scoring_scheme_str == '2,-6,-5,-3':
        return 0.578
    elif scoring_scheme_str == '1,-4,-6,-1':
        return 0.812

    # If the scoring scheme doesn't match a previously known one, we will use the C++ code to get
    # an error rate estimate.
    else:
        error_rate_str = get_random_sequence_alignment_error_rates(1000, 100, scoring_scheme)
        return float(error_rate_str.split('\n')[1].split('\t')[6])

def load_sam_alignments(sam_filename, read_dict, reference_dict, scoring_scheme, threads):
    '''
    This function returns a list of Alignment objects from the given SAM file.
    '''
    if VERBOSITY > 0:
        print('Loading alignments')
        print('------------------')

    # Load the SAM lines into a list.
    sam_lines = []
    sam_file = open(sam_filename, 'r')
    for line in sam_file:
        line = line.strip()
        if line and not line.startswith('@') and line.split('\t', 3)[2] != '*':
            sam_lines.append(line)
    num_alignments = sum(1 for line in open(sam_filename) if not line.startswith('@'))
    print_progress_line(0, num_alignments)

    # If single-threaded, just do the work in a simple loop.
    threads = 1 # TEMP
    sam_alignments = []
    if threads == 1:
        for line in sam_lines:
            sam_alignments.append(Alignment(line, read_dict, reference_dict, scoring_scheme))
            if VERBOSITY > 0:
                print_progress_line(len(sam_alignments), num_alignments)

    # # If multi-threaded, use processes.
    # else:
    #     sam_line_groups = chunkify(sam_lines, threads)
    #     manager = Manager()
    #     workers = []
    #     sam_alignments = manager.list([])
    #     for sam_line_group in sam_line_groups:
    #         child = Process(target=make_alignments, args=(sam_line_group, read_dict,
    #                                                       reference_dict, scoring_scheme,
    #                                                       sam_alignments))
    #         child.start()
    #         workers.append(child)
    #     while any(i.is_alive() for i in workers):
    #         time.sleep(0.1)
    #         if VERBOSITY > 0:
    #             print_progress_line(len(sam_alignments), num_alignments)
    #     for worker in workers:
    #         worker.join()
    #     sam_alignments = sam_alignments._getvalue()

    # At this point, we should have loaded num_alignments alignments. But check to make sure and
    # fix up the progress line if any didn't load.
    if VERBOSITY > 0:
        if len(sam_alignments) < num_alignments:
            print_progress_line(len(sam_alignments), len(sam_alignments))
        print('\n')

    return sam_alignments

# def chunkify(full_list, pieces):
#     '''
#     http://stackoverflow.com/questions/2130016/
#     splitting-a-list-of-arbitrary-size-into-only-roughly-n-equal-parts
#     '''
#     return [full_list[i::pieces] for i in xrange(pieces)]

# def make_alignments(sam_lines, read_dict, reference_dict, scoring_scheme, alignments):
#     '''
#     Produces alignments from SAM lines and deposits them in a managed list.
#     '''
#     for line in sam_lines:
#         alignments.append(Alignment(line, read_dict, reference_dict, scoring_scheme))

def count_depth_and_errors_per_base(references, reference_dict, alignments):
    '''
    Counts up the depth and errors for each base of each reference and stores the counts in the
    Reference objects.
    '''
    if VERBOSITY > 0:
        print('Counting depth and errors')
        print('-------------------------')
        print_progress_line(0, len(alignments))

    for ref in references:
        ref_length = ref.get_length()
        ref.depths = [0] * ref_length
        ref.mismatch_counts = [0] * ref_length
        ref.insertion_counts = [0] * ref_length
        ref.deletion_counts = [0] * ref_length
        ref.error_rates = [None] * ref_length
        ref.alignment_count = 0

    for i, alignment in enumerate(alignments):
        ref = reference_dict[alignment.ref.name]
        ref.alignment_count += 1
        for j in range(alignment.ref_start_pos, alignment.ref_end_pos):
            ref.depths[j] += 1
            if ref.error_rates[j] is None:
                ref.error_rates[j] = 0.0
        for j in alignment.ref_mismatch_positions:
            ref.mismatch_counts[j] += 1
        for j in alignment.ref_insertion_positions:
            ref.insertion_counts[j] += 1
        for j in alignment.ref_deletion_positions:
            ref.deletion_counts[j] += 1
        if VERBOSITY > 0:
            print_progress_line(i+1, len(alignments))

    if VERBOSITY > 0:
        print('\n')
        base_sum = sum([x.get_length() for x in references])
        finished_bases = 0
        print('Totalling depth and errors')
        print('--------------------------')
        print_progress_line(finished_bases, base_sum)

    for ref in references:
        ref_length = ref.get_length()
        for i in range(ref_length):
            if ref.depths[i] > 0:
                error_count = ref.mismatch_counts[i] + ref.insertion_counts[i] + \
                              ref.deletion_counts[i]
                ref.error_rates[i] = error_count / ref.depths[i]
            if VERBOSITY > 0:
                finished_bases += 1
                if finished_bases % 10 == 0:
                    print_progress_line(finished_bases, base_sum)

    if VERBOSITY > 0:
        print_progress_line(base_sum, base_sum)
        print('\n')


def count_depth_and_errors_per_window(references, window_size):
    '''
    Counts up the depth and errors for each window of each reference and stores the counts in the
    Reference objects.
    '''
    for ref in references:
        ref_length = ref.get_length()
        window_count = max(1, int(round(ref_length / window_size)))
        ref.window_size = ref_length / window_count

        ref.window_starts = []
        ref.window_depths = []
        ref.window_error_rates = []
        ref.min_window_depth = None
        ref.min_window_error_rate = None
        ref.max_window_depth = 0.0
        ref.max_window_error_rate = 0.0

        for i in xrange(window_count):
            window_start = int(round(ref.window_size * i))
            window_end = int(round(ref.window_size * (i + 1)))
            ref.window_starts.append(window_start)
            this_window_size = window_end - window_start
            this_window_pos_with_error_rate = 0

            total_window_depth = 0
            total_window_error_rate = None
            for j in xrange(window_start, window_end):
                total_window_depth += ref.depths[j]
                if ref.error_rates[j] is not None:
                    this_window_pos_with_error_rate += 1
                    if total_window_error_rate is None:
                        total_window_error_rate = 0.0
                    total_window_error_rate += ref.error_rates[j]

            window_depth = total_window_depth / this_window_size
            if total_window_error_rate is None:
                window_error_rate = None
            else:
                window_error_rate = total_window_error_rate / this_window_pos_with_error_rate

            ref.window_depths.append(window_depth)
            ref.window_error_rates.append(window_error_rate)

            if ref.min_window_depth is None:
                ref.min_window_depth = window_depth
            else:
                ref.min_window_depth = min(window_depth, ref.min_window_depth)

            if ref.min_window_error_rate is None:
                ref.min_window_error_rate = window_error_rate
            else:
                ref.min_window_error_rate = min(window_error_rate, ref.min_window_error_rate)

            ref.max_window_depth = max(window_depth, ref.max_window_depth)
            ref.max_window_error_rate = max(window_error_rate, ref.max_window_error_rate)

def determine_thresholds(scoring_scheme, references):
    '''
    This function sets thresholds for error rate and depth. Error rate thresholds are set once for
    all references, while depth thresholds are per-reference.
    '''
    if VERBOSITY > 0:
        print('Setting error and depth thresholds')
        print('----------------------------------')

    # Find the median of all error rates.
    all_error_rates = []
    for ref in references:
        all_error_rates += [x for x in ref.error_rates if x is not None]

    mean_error_rate = get_mean(all_error_rates)
    if VERBOSITY > 0:
        print('Mean error rate:            ', float_to_str(mean_error_rate, 2, 100.0) + '%')
    random_seq_error_rate = get_random_sequence_error_rate(scoring_scheme)
    if VERBOSITY > 0:
        print('Random alignment error rate:', float_to_str(random_seq_error_rate, 2, 100.0) + '%')
        print()

    # The median error rate should not be as big as the random alignment error rate. If it is, then
    # we set the 
    if mean_error_rate >= random_seq_error_rate:
        high_error_rate = random_seq_error_rate * 0.9
        very_high_error_rate = random_seq_error_rate
    
    # In the expected case where the median error rate is below the random alignment error rate, we
    # set the thresholds between these values.
    else:
        difference = random_seq_error_rate - mean_error_rate
        high_error_rate = mean_error_rate + (0.2 * difference)
        very_high_error_rate = mean_error_rate + (0.3 * difference)

    if VERBOSITY > 0:
        print('Error rate threshold 1:     ', float_to_str(high_error_rate, 2, 100.0) + '%')
        print('Error rate threshold 2:     ', float_to_str(very_high_error_rate, 2, 100.0) + '%')
        print()








    if VERBOSITY > 0:
        print()

    return high_error_rate, very_high_error_rate



def get_mean(num_list):
    '''
    This function returns the mean of the given list of numbers.
    '''
    if not num_list:
        return None
    return sum(num_list) / len(num_list)



# def get_median(num_list):
#     '''
#     Returns the median of the given list of numbers.
#     '''
#     count = len(num_list)
#     if count == 0:
#         return 0.0
#     sorted_list = sorted(num_list)
#     if count % 2 == 0:
#         return (sorted_list[count // 2 - 1] + sorted_list[count // 2]) / 2.0
#     else:
#         return sorted_list[count // 2]

# def get_median_and_mad(num_list):
#     '''
#     Returns the median and MAD of the given list of numbers.
#     '''
#     if not num_list:
#         return None, None
#     if len(num_list) == 1:
#         return num_list[0], None

#     median = get_median(num_list)
#     absolute_deviations = [abs(x - median) for x in num_list]
#     mad = 1.4826 * get_median(absolute_deviations)
#     return median, mad

def produce_console_output(references):
    '''
    Write a summary of the results to std out.
    '''
    for ref in references:
        print('Results: ' + ref.name)
        print('-' * (len(ref.name) + 9))
        ref_length = ref.get_length()
        max_v = max(100, ref_length)

        print('Length:         ', int_to_str(ref_length, max_v) + ' bp')
        print('Alignments:     ', int_to_str(ref.alignment_count, max_v))
        print('Min depth:      ', float_to_str(ref.min_window_depth, 2, max_v))
        print('Max depth:      ', float_to_str(ref.max_window_depth, 2, max_v))
        print('Min error rate: ', float_to_str(ref.min_window_error_rate * 100.0, 2, max_v) + '%')
        print('Max error rate: ', float_to_str(ref.max_window_error_rate * 100.0, 2, max_v) + '%')

        print()

def clean_str_for_filename(filename):
    '''
    This function removes characters from a string which would not be suitable in a filename.
    It also turns spaces into underscores, because filenames with spaces can occasionally cause
    issues.
    http://stackoverflow.com/questions/295135/turn-a-string-into-a-valid-filename-in-python
    '''
    valid_chars = "-_.() %s%s" % (string.ascii_letters, string.digits)
    filename_valid_chars = ''.join(c for c in filename if c in valid_chars)
    return filename_valid_chars.replace(' ', '_')

def add_ref_name_to_output_prefix(ref, output_prefix, ending):
    clean_ref_name = clean_str_for_filename(ref.name)
    if output_prefix.endswith('/'):
        return output_prefix + clean_ref_name + ending
    else:
        return output_prefix + '_' + clean_ref_name + ending

def produce_window_tables(references, window_tables_prefix):
    '''
    Write tables of depth and error rates per reference window.
    '''
    if VERBOSITY > 0:
        print('Saving window tables')
        print('--------------------')

    for ref in references:
        window_table_filename = add_ref_name_to_output_prefix(ref, window_tables_prefix, '.txt')
        table = open(window_table_filename, 'w')
        table.write('\t'.join(['Window start',
                               'Window end',
                               'Mean depth',
                               'Mean error rate']) + '\n')
        window_count = len(ref.window_starts)
        for i in xrange(window_count):
            if i + 1 == window_count:
                window_end = ref.get_length()
            else:
                window_end = ref.window_starts[i+1]
            table.write('\t'.join([str(ref.window_starts[i]),
                                   str(window_end),
                                   str(ref.window_depths[i]),
                                   str(ref.window_error_rates[i])]) + '\n')
        if VERBOSITY > 0:
            print(window_table_filename)
    if VERBOSITY > 0:
        print()

def produce_base_tables(references, base_tables_prefix):
    '''
    Write tables of depth and error counts per reference base.
    '''
    if VERBOSITY > 0:
        print('Saving base tables')
        print('------------------')

    for ref in references:
        base_table_filename = add_ref_name_to_output_prefix(ref, base_tables_prefix, '.txt')
        table = open(base_table_filename, 'w')
        table.write('\t'.join(['Base',
                               'Read depth',
                               'Mismatches',
                               'Deletions',
                               'Insertions']) + '\n')
        for i in xrange(ref.get_length()):
            table.write('\t'.join([str(i+1),
                                   str(ref.depths[i]),
                                   str(ref.mismatch_counts[i]),
                                   str(ref.deletion_counts[i]),
                                   str(ref.insertion_counts[i])]) + '\n')
        if VERBOSITY > 0:
            print(base_table_filename)
    if VERBOSITY > 0:
        print()


def produce_html_files(references, html_prefix, high_error_rate, very_high_error_rate):
    '''
    Write html files containing plots of results.
    '''
    if VERBOSITY > 0:
        print('Saving html plots')
        print('-----------------')

    import plotly.offline as py
    import plotly.graph_objs as go

    for ref in references:
        error_rate_html_filename = add_ref_name_to_output_prefix(ref, html_prefix,
                                                                 '_error_rate.html')
        depth_html_filename = add_ref_name_to_output_prefix(ref, html_prefix, '_depth.html')

        half_window_size = ref.window_size / 2
        x = []
        error_rate_y = []
        depth_y = []
        for i, window_start in enumerate(ref.window_starts):
            x.append(window_start + half_window_size)
            if ref.window_error_rates[i] is None:
                error_rate_y.append(None)
            else:
                error_rate_y.append(round(ref.window_error_rates[i], 2))
            depth_y.append(ref.window_depths[i])
        if all(y is None for y in error_rate_y):
            continue

        max_error_rate = max(error_rate_y)
        max_depth = max(depth_y)

        error_trace = go.Scatter(x=x,
                                 y=error_rate_y,
                                 mode='lines',
                                 line=dict(color='rgb(120, 0, 0)'))
        data = [error_trace]

        layout = dict(title='Error rate: ' + ref.name,
                      autosize=False,
                      width=1400,
                      height=500,
                      xaxis=dict(title='Reference position',
                                 range=[0, ref.get_length()],
                                 rangeslider=dict(),
                                 type='linear'),
                      yaxis=dict(title='Error rate',
                                 titlefont=dict(color='rgb(120, 0, 0)'),
                                 ticksuffix='%',
                                 range=[0.0, max_error_rate * 1.05]))

        fig = dict(data=data, layout=layout)
        py.plot(fig, filename=error_rate_html_filename, auto_open=False)
        if VERBOSITY > 0:
            print(error_rate_html_filename)

        depth_trace = go.Scatter(x=x,
                                 y=depth_y,
                                 mode='lines',
                                 line=dict(color='rgb(0, 120, 0)'))
        data = [depth_trace]
        layout.update(title='Depth: ' + ref.name,
                      yaxis=dict(title='Depth',
                                 titlefont=dict(color='rgb(0, 120, 0)'),
                                 range=[0.0, max_depth * 1.05]))

        fig = dict(data=data, layout=layout)
        py.plot(fig, filename=depth_html_filename, auto_open=False)
        if VERBOSITY > 0:
            print(depth_html_filename)

    if VERBOSITY > 0:
        print()



class Alignment(object):
    '''
    This class describes an alignment between a long read and a reference.
    '''
    def __init__(self, sam_line, read_dict, reference_dict, scoring_scheme):

        # Grab the important parts of the alignment from the SAM line.
        sam_parts = sam_line.split('\t')
        self.rev_comp = bool(int(sam_parts[1]) & 0x10)
        cigar_parts = re.findall(r'\d+\w', sam_parts[5])
        cigar_types = [x[-1] for x in cigar_parts]
        cigar_counts = [int(x[:-1]) for x in cigar_parts]

        self.read = read_dict[sam_parts[0]]
        read_len = self.read.get_length()
        self.read_start_pos = self.get_start_soft_clips(cigar_parts)
        self.read_end_pos = self.read.get_length() - self.get_end_soft_clips(cigar_parts)
        self.read_end_gap = self.get_end_soft_clips(cigar_parts)

        self.ref = reference_dict[get_nice_header(sam_parts[2])]
        ref_len = self.ref.get_length()
        self.ref_start_pos = int(sam_parts[3]) - 1
        self.ref_end_pos = self.ref_start_pos
        for i in xrange(len(cigar_types)):
            self.ref_end_pos += get_ref_shift_from_cigar_part(cigar_types[i], cigar_counts[i])
        if self.ref_end_pos > ref_len:
            self.ref_end_pos = ref_len
        self.ref_end_gap = ref_len - self.ref_end_pos

        self.ref_mismatch_positions = []
        self.ref_deletion_positions = []
        self.ref_insertion_positions = []

        # Remove the soft clipping parts of the CIGAR for tallying.
        if cigar_types[0] == 'S':
            cigar_types.pop(0)
            cigar_counts.pop(0)
        if cigar_types and cigar_types[-1] == 'S':
            cigar_types.pop()
            cigar_counts.pop()
        if not cigar_types:
            return

        if self.rev_comp:
            read_seq = reverse_complement(self.read.sequence)
        else:
            read_seq = self.read.sequence

        read_i = self.read_start_pos
        ref_i = self.ref_start_pos

        for i in xrange(len(cigar_types)):
            cigar_count = cigar_counts[i]
            cigar_type = cigar_types[i]
            if cigar_type == 'I':
                self.ref_insertion_positions += [ref_i]*cigar_count
                read_i += cigar_count
            elif cigar_type == 'D':
                for i in xrange(cigar_count):
                    self.ref_deletion_positions.append(ref_i + i)
                ref_i += cigar_count
            else: # match/mismatch
                for _ in xrange(cigar_count):
                    # If all is good with the CIGAR, then we should never end up with a sequence
                    # index out of the sequence range. But a CIGAR error (which has occurred in
                    # GraphMap) can cause this, so check here.
                    if read_i >= read_len or ref_i >= ref_len:
                        break
                    if read_seq[read_i] != self.ref.sequence[ref_i]:
                        self.ref_mismatch_positions.append(ref_i)
                    read_i += 1
                    ref_i += 1

    def __repr__(self):
        read_start, read_end = self.read_start_end_positive_strand()
        return_str = self.read.name + ' (' + str(read_start) + '-' + str(read_end) + ', '
        if self.rev_comp:
            return_str += 'strand: -), '
        else:
            return_str += 'strand: +), '
        return_str += self.ref.name + ' (' + str(self.ref_start_pos) + '-' + \
                      str(self.ref_end_pos) + ')'
        error_count = len(self.ref_mismatch_positions) + len(self.ref_deletion_positions) + \
                      len(self.ref_insertion_positions)
        return_str += ', errors = ' + int_to_str(error_count)
        return return_str

    def get_start_soft_clips(self, cigar_parts):
        '''
        Returns the number of soft-clipped bases at the start of the alignment.
        '''
        if cigar_parts[0][-1] == 'S':
            return int(cigar_parts[0][:-1])
        else:
            return 0

    def get_end_soft_clips(self, cigar_parts):
        '''
        Returns the number of soft-clipped bases at the start of the alignment.
        '''
        if cigar_parts[-1][-1] == 'S':
            return int(cigar_parts[-1][:-1])
        else:
            return 0

    def read_start_end_positive_strand(self):
        '''
        This function returns the read start/end coordinates for the positive strand of the read.
        For alignments on the positive strand, this is just the normal start/end. But for
        alignments on the negative strand, the coordinates are flipped to the other side.
        '''
        if not self.rev_comp:
            return self.read_start_pos, self.read_end_pos
        else:
            start = self.read.get_length() - self.read_end_pos
            end = self.read.get_length() - self.read_start_pos
            return start, end


def get_ref_shift_from_cigar_part(cigar_type, cigar_count):
    '''
    This function returns how much a given cigar moves on a reference.
    Examples:
      * '5M' returns 5
      * '5S' returns 0
      * '5D' returns 5
      * '5I' returns 0
    '''
    if cigar_type == 'M' or cigar_type == 'D':
        return cigar_count
    else:
        return 0

if __name__ == '__main__':
    main()