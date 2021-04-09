import os
import time
import logging
import itertools
import cloudpickle
import numpy as np
from multiprocessing import Process, Queue
from pathlib import Path
from pyaugmecon.options import Options
from pyaugmecon.model import Model
from pyaugmecon.helper import Helper


def solve_chunk(
        cp,
        opts: Options,
        model: Model,
        result_q: Queue):

    flag = {}
    jump = 0
    pareto_sols = []

    cp_start = cp[0][0]
    cp_end = cp[opts.gp - 1][0]
    model.progress.set_message('finding solutions')
    model.unpickle()

    for c in cp:
        model.progress.increment()

        def do_jump(i, jump):
            return min(jump, abs(cp_end - i))

        def bypass_range(i):
            if i == 0:
                return range(c[i], c[i] + 1)
            elif model.min_obj:
                return range(c[i] - b[i], c[i] + 1)
            else:
                return range(c[i], c[i] + b[i] + 1)

        def early_exit_range(i):
            if i == 0:
                return range(c[i], c[i] + 1)
            elif model.min_obj:
                return range(c[i], cp_start)
            else:
                return range(c[i], cp_end)

        def set_flag_array(flag_range, value, objfun_iter2):
            indices = [tuple([n for n in flag_range(o)])
                       for o in objfun_iter2]
            iter = list(itertools.product(*indices))

            for i in iter:
                flag[i] = value

        if flag.get(c, 0) != 0 and jump == 0:
            jump = do_jump(c[0] - 1, flag[c])

        if jump > 0:
            jump = jump - 1
            continue

        for o in model.iter_obj2:
            model.model.e[o + 2] = model.e[o, c[o]]

        model.activate_objfun(1)
        model.solve()
        model.models_solved.increment()

        if (model.is_infeasible()):
            set_flag_array(early_exit_range, opts.gp, model.iter_obj2)
            jump = do_jump(c[0], opts.gp)
            continue
        elif (model.is_status_ok() and model.is_feasible()):
            b = []

            for i in model.iter_obj2:
                step = model.obj_range[i] / (opts.gp - 1)
                slack = round(model.model.Slack[i + 2].value, 3)
                b.append(int(slack/step))

            set_flag_array(bypass_range, b[0] + 1, model.iter_obj2)
            jump = do_jump(c[0], b[0])

        # From this point onward the code is about saving and sorting out
        # unique Pareto Optimal Solutions
        temp_list = []

        # If range is to be considered or not, it should also be
        # changed here (otherwise, it produces artifact solutions)
        temp_list.append(round(model.model.obj_list[1]() - opts.eps
                         * sum(model.model.Slack[o1].value
                               / model.obj_range[o1 - 2]
                               for o1 in model.model.Os), 2))

        for o in model.iter_obj2:
            temp_list.append(round(model.model.obj_list[o + 2](), 2))
        pareto_sols.append(tuple(temp_list))

    result_q.put(pareto_sols)


class PyAugmecon(object):

    def __init__(
            self,
            model,
            opts={},
            solver_opts={}):

        self.opts = Options(opts, solver_opts)
        self.model = Model(model, self.opts)

        # Define basic process parameters
        self.time_created = time.strftime("%Y%m%d-%H%M%S")
        self.name = self.opts.name + '_' + str(self.time_created)
        self.start_time = time.time()

        # Configure logging
        if not os.path.exists(self.opts.logdir):
            os.makedirs(self.opts.logdir)
        logdir = f'{Path().absolute()}/{self.opts.logdir}/'
        logfile = f'{logdir}{self.name}.log'
        logging.basicConfig(format='%(message)s',
                            filename=logfile, level=logging.INFO)

    def discover_pareto(self):
        if self.model.min_obj:
            grid_range = list(reversed(range(self.opts.gp)))
        else:
            grid_range = range(self.opts.gp)

        indices = [tuple([n for n in grid_range])
                   for o in self.model.iter_obj2]
        self.cp = list(itertools.product(*indices))
        self.cp = [i[::-1] for i in self.cp]

        # Divide grid points over threads
        self.cp_presplit = [self.cp[i:i + self.opts.gp]
                            for i in range(0, len(self.cp), self.opts.gp)]
        remainder = self.opts.gp % self.opts.cpu_count
        take = int((self.opts.gp - remainder) / self.opts.cpu_count)
        self.cp_split = []

        start = -take
        for i in range(self.opts.cpu_count):
            start += take
            end = start + take + remainder
            self.cp_split.append([i for sublist in self.cp_presplit[start:end]
                                  for i in sublist])
            if i == 0:
                start += remainder
                remainder = 0

        result_q = Queue()
        self.model.pickle()

        procs = [Process(
            target=solve_chunk,
            args=(
                cp,
                self.opts,
                self.model,
                result_q))
            for cp in self.cp_split]

        for p in procs:
            p.start()

        results = [result_q.get() for p in procs]

        for p in procs:
            p.join()

        self.model.clean()

        self.pareto_sols_temp = [i for sublist in results for i in sublist]

    def find_unique_sols(self):
        self.unique_pareto_sols = list(set(tuple(self.pareto_sols_temp)))
        self.num_unique_pareto_sols = len(self.unique_pareto_sols)
        self.pareto_sols = np.zeros(
            (self.num_unique_pareto_sols, self.model.n_obj,))

        for item_index, item in enumerate(self.unique_pareto_sols):
            for o in range(self.model.n_obj):
                self.pareto_sols[item_index, o] = item[o]

    def solve(self):
        self.model.construct_payoff()
        self.model.find_obj_range()
        self.model.convert_prob()
        self.discover_pareto()
        self.find_unique_sols()

        Helper.clear_line()
        self.runtime = round(time.time() - self.start_time, 2)
        print(f'Solved {self.model.models_solved.value()} models for '
              f'{self.num_unique_pareto_sols} unique solutions in '
              f'{self.runtime} seconds')
