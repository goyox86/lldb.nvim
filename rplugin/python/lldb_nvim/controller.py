import os, sys
import lldb
from threading import Thread

from .vim_buffers import VimBuffers
from .session import Session

class Controller(Thread):
  """ Handles LLDB events and commands. """

  CTRL_VOICE = 238 # just a number of my choice
  def __init__(self, vimx):
    """ Creates the LLDB SBDebugger object and initializes the VimBuffers class. """
    from Queue import Queue
    import logging
    self.logger = logging.getLogger(__name__)
    self.logger.setLevel(logging.INFO)

    self._target = None
    self._process = None
    self.in_queue = Queue()
    self.out_queue = Queue()

    self.dbg = lldb.SBDebugger.Create()
    self.interpreter = self.dbg.GetCommandInterpreter()

    self.listener = lldb.SBListener("the_ear")
    self.interrupter = lldb.SBBroadcaster("the_mouth")
    self.interrupter.AddListener(self.listener, self.CTRL_VOICE)

    self.vimx = vimx # represents the parent Middleman object
    self.buffers = VimBuffers(vimx)
    self.session = Session(vimx)
    super(Controller, self).__init__()

  def safe_call(self, method, args=[], sync=False): # safe_ marks thread safety
    if self.dbg is None:
      self.logger.critical("Debugger was terminated!" +
          (" Attempted calling %s" % method.func_name if method else ""))
      return
    self.in_queue.put((method, args, sync))
    interrupt = lldb.SBEvent(self.CTRL_VOICE, "the_sound")
    self.interrupter.BroadcastEvent(interrupt)
    if sync: # DECIDE timeout?
      return self.out_queue.get(True)

  def safe_execute(self, cmd, args):
    self.safe_call(self.exec_command, [cmd, ' '.join(args)])

  def safe_exit(self):
    self.safe_call(None)
    self.join()

  def complete_command(self, arg, line, pos):
    """ Returns a list of viable completions for line, and cursor at pos. """
    result = lldb.SBStringList()
    num = self.interpreter.HandleCompletion(line, pos, 1, -1, result)

    if num == -2: # encountered history repeat character ?
      pass

    if result.GetSize() > 0:
      cands = [result.GetStringAtIndex(x) for x in range(result.GetSize())]
      if cands[0] == '' and arg != '':
        if not cands[1].startswith(arg) or not cands[-1].startswith(arg):
          return []
      return cands[1:]
    else:
      return []

  def update_buffers(self, jump2pc=True, buf=None):
    """ Update lldb buffers and signs placed in source files.
        @param jump2pc
            Whether or not to move the cursor to the program counter (PC).
        @param buf
            If None, all buffers and signs excepts breakpoints would be updated.
            If '!all', all buffers incl. breakpoints would be updated.
            Otherwise, update only the specified buffer.
    """
    excl = ['breakpoints']
    commander = self.get_command_result
    target = self.get_target()
    if buf is None:
      self.buffers.update(target, commander, jump2pc, excl)
    elif buf == '!all':
      self.buffers.update(target, commander, jump2pc)
    else:
      self.buffers.update_buffer(buf, target, commander)

  def get_target(self):
    if self._target and self._target.IsValid():
      return self._target
    target = self.dbg.GetSelectedTarget()
    if target and target.IsValid():
      self._target = target
      target.broadcaster.AddListener(self.listener,
          lldb.SBTarget.eBroadcastBitBreakpointChanged)
          # TODO WatchpointChanged
      return target
    return None

  def get_process(self):
    if self._process and self._process.IsValid():
      return self._process
    target = self.get_target()
    if target and target.process and target.process.IsValid():
      self._process = target.process
      self._process.broadcaster.AddListener(self.listener,
          lldb.SBProcess.eBroadcastBitStateChanged)
          # TODO STDOUT, STDERR
      return self._process
    return None

  def do_frame(self, args):
    """ Handle 'frame' command. """
    self.exec_command("frame", args)
    if args.startswith('s'): # select
      self.update_buffers()

  def do_thread(self, args):
    """ Handle 'thread' command. """
    self.exec_command("thread", args)
    if args.startswith('se'): # select
      self.update_buffers()

  def do_process(self, args):
    """ Handle 'process' command. """
    process = self.get_process()
    if args.startswith("la"): # launch
      if process:
        process.Destroy()

      (success, result) = self.get_command_result("process", args)
      if not success:
        self.vimx.log("Error during launch: " + str(result))
        return
      self.get_process() # add listener
      self.vimx.log("%s" % result, 0)
    else:
      self.exec_command("process", args)

  def do_target(self, args):
    """ Handle 'target' command. """
    (success, result) = self.get_command_result("target", args)
    if not success:
      self.vimx.log(str(result))
    elif args.startswith('d'): # delete
      self.update_buffers(buf='!all')
    self.vimx.log(str(result), 0)
    self.get_target()

  def do_disassemble(self, args):
    self.buffers._content_map['disassembly'][1][1] = args
    self.update_buffers(buf='disassembly')

  def do_breakswitch(self, bufnr, line):
    """ Switch breakpoint at the specified line in the buffer. """
    key = (bufnr, line)
    if self.buffers.bp_list.has_key(key):
      bps = self.buffers.bp_list[key]
      args = "delete %s" % " ".join([str(b.GetID()) for b in bps])
    else:
      path = self.vimx.get_buffer_name(bufnr)
      args = "set -f %s -l %d" % (path, line)
    self.do_breakpoint(args)

  def do_breakpoint(self, args):
    """ Handle breakpoint command with command interpreter. """
    self.exec_command("breakpoint", args)

  def get_command_result(self, command, args=""):
    """ Run command in the command interpreter and returns (success, output) """
    result = lldb.SBCommandReturnObject()
    cmd = "%s %s" % (command, args)

    self.interpreter.HandleCommand(cmd, result)
    return (result.Succeeded(), result.GetOutput() if result.Succeeded() else result.GetError())

  def exec_command(self, command, args, show_result=True, jump2pc=False):
    """ Run command in interpreter and possibly log the result as a vim message """
    (success, output) = self.get_command_result(command, args)
    if not success:
      self.vimx.log(output)
    elif show_result and len(output) > 0:
      self.vimx.log(output, 0)

  def run(self):
    import traceback
    from Queue import Empty
    to_count = 0
    while True:
      event = lldb.SBEvent()
      if self.listener.WaitForEvent(30, event): # 30 second timeout

        def event_matches(broadcaster, skip=True):
          if event.BroadcasterMatchesRef(broadcaster):
            if skip:
              while self.listener.GetNextEventForBroadcaster(broadcaster, event):
                pass
            return True
          return False

        if event_matches(self.interrupter, skip=False):
          try:
            method, args, sync = self.in_queue.get(False)
            if method is None:
              break
            self.logger.info('Calling %s with %s' % (method.func_name, repr(args)))
            ret = method(*args)
            if sync:
              self.out_queue.put(ret)
          except Empty:
            self.logger.info('Empty interrupt!')
          except Exception:
            self.logger.critical(traceback.format_exc())
        elif event_matches(self._target.broadcaster):
          self.update_buffers(buf='breakpoints')
        elif event_matches(self._process.broadcaster):
          self.update_buffers(buf='!all')
      else: # Timed out
        to_count += 1
        if to_count > 172800: # 60 days worth idleness! barrier to prevent infinite loop
          self.logger.critical('Broke the loop barrier!')
          break
    self.dbg.Terminate()
    self.dbg = None
    self.logger.info('Terminated!')
