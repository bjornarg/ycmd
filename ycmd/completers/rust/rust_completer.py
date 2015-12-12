#!/usr/bin/env python
#
# Copyright (C) 2015 Google Inc.
#
# This file is part of YouCompleteMe.
#
# YouCompleteMe is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# YouCompleteMe is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with YouCompleteMe.  If not, see <http://www.gnu.org/licenses/>.

import logging
import os
import subprocess

from threading import Lock
from tempfile import NamedTemporaryFile

from ycmd import responses, utils
from ycmd.completers.completer import Completer

_logger = logging.getLogger( __name__ )

BINARY_NOT_FOUND_MESSAGE = ( 'racer not found.' )
RUST_SRC_PATH_NOT_FOUND_MESSAGE = ( 'rust source path not found.'
                                    'Please set RUST_SRC_PATH to source path '
                                    'of rust' )
PATH_TO_RACER_BINARY = os.path.join(
  os.path.abspath( os.path.dirname( __file__ ) ),
  '..', '..', '..', 'third_party', 'racer',
  'target', 'release', 'racer' + ( '.exe' if utils.OnWindows() else '' ) )

class RustCompleter( Completer ):
    def __init__( self, user_options ):
        super( RustCompleter, self ).__init__( user_options )

        self._lock = Lock()

        self._binary = _FindRacerBinary( user_options )

        if self._binary is None:
            _logger.error( BINARY_NOT_FOUND_MESSAGE )
            raise RuntimeError( BINARY_NOT_FOUND_MESSAGE )

        self._rust_source = _FindRustSource( user_options )

        if self._rust_source is None:
            _logger.error( RUST_SRC_PATH_NOT_FOUND_MESSAGE )
            raise RuntimeError( RUST_SRC_PATH_NOT_FOUND_MESSAGE )

        self._environ = os.environ.copy()
        self._environ[ 'RUST_SRC_PATH' ] = self._rust_source

        self._StartServer()

        _logger.info( 'Enabling rust completion' )


    def ServerIsRunning( self ):
        return self._racer_handle.poll() == None


    def _StopServer( self ):
        with self._lock:
            self._racer_handle.terminate()
        _logger.info( 'Stopped racer server' )


    def _StartServer( self ):
        with self._lock:
            self._racer_handle = _StartRacer( self._binary, self._environ )
        _logger.info( 'Started racer server' )


    def _RestartServer( self ):
        with self._lock:
            self._racer_handle.terminate()
            self._racer_handle.communicate()
            self._racer_handle = _StartRacer( self._binary, self._environ )
        _logger.info( 'Restarted racer server' )


    def _SendRequest(self, command, arguments=None):
        if not self.ServerIsRunning():
            raise RuntimeError( 'racer server not running' )
        request = [ command ]
        if arguments is not None:
            request.extend( [ str(argument) for argument in arguments ] )
        self._racer_handle.stdin.write( ' '.join( request ) )
        self._racer_handle.stdin.write( '\n' )


    def _ReadResponse(self):
        response = []
        while True:
            output = self._racer_handle.stdout.readline().strip()
            if output == 'END':
                break
            response.append(output)
        return response


    def SupportedFiletypes(self):
        return [ 'rust' ]


    def ComputeCandidatesInner(self, request_data):
        with self._lock:
            filename = request_data[ 'filepath' ]
            if not filename:
                return

            contents = request_data[ 'file_data' ][ filename ][ 'contents' ]
            tmpfile = NamedTemporaryFile( delete=False )
            tmpfile.write( utils.ToUtf8IfNeeded( contents ) )
            tmpfile.close()

            self._SendRequest( 'complete', [
              request_data[ 'line_num' ],
              request_data[ 'start_column' ],
              request_data[ 'filepath' ],
              tmpfile.name,
            ] )

            response = self._ReadResponse()

            os.unlink( tmpfile.name )

            matches = []
            for line in response:
                parsed = _ParseCompleteResponse( line )
                if parsed is not None:
                    matches.append( _ConvertCompletionData( parsed ) )
            return matches


    def _GoToDefinition(self, request_data):
        with self._lock:
            self._SendRequest( 'find-definition', [
                request_data[ 'line_num' ],
                request_data[ 'column_num' ],
                request_data[ 'filepath' ],
            ] )

            response = self._ReadResponse()

            match = None
            for line in response:
                parsed = _ParseCompleteResponse( line )
                if parsed is not None:
                    return _ConvertGoToData( parsed )
            raise RuntimeError( 'Could not find definition' )


    def GetSubcommandsMap( self ):
        return {
          'GoToDefinition':  ( lambda self, request_data:
                               self._GoToDefinition( request_data ) ),
          'GoToDeclaration': ( lambda self, request_data:
                               self._GoToDefinition( request_data ) ),
          'GoTo':            ( lambda self, request_data:
                               self._GoToDefinition( request_data ) ),
          'StartServer':     ( lambda self, request_data:
                               self._StartServer() ),
          'StopServer':      ( lambda self, request_data:
                               self._StopServer() ),
          'RestartServer':   ( lambda self, request_data:
                               self._RestartServer() ),
          'ServerRunning':   ( lambda self, request_data:
                               self.ServerIsRunning() ),
        }


def _ParseCompleteResponse( line ):
    prefix = 'MATCH '
    if line.startswith( prefix ):
        parts = line[ len( prefix ): ].split( ',', 5 )
        return {
          'name':       utils.ToUtf8IfNeeded( parts[0] ),
          'line_num':   int( parts[1] ),
          'column_num': int( parts[2] ) + 1,
          'filepath':   utils.ToUtf8IfNeeded( parts[3] ),
          'kind':       utils.ToUtf8IfNeeded( parts[4] ),
          'snippet':    utils.ToUtf8IfNeeded( parts[5] ),
        }
    return None


def _ConvertCompletionData( completion_data ):
    return responses.BuildCompletionData(
      insertion_text  = completion_data[ 'name' ],
      menu_text       = completion_data[ 'name' ],
      kind            = completion_data[ 'kind' ],
      extra_menu_info = completion_data[ 'snippet' ],
    )


def _ConvertGoToData( completion_data ):
    return responses.BuildGoToResponse(
      line_num   = completion_data[ 'line_num' ],
      column_num = completion_data[ 'column_num' ],
      filepath   = completion_data[ 'filepath' ],
    )

def _FindRacerBinary( user_options ):
    if user_options.get( 'racer_binary_path' ):
        if os.path.isfile( user_options[ 'racer_binary_path' ] ):
            return user_options[ 'racer_binary_path' ]
        else:
            return None

    if os.path.isfile( PATH_TO_RACER_BINARY ):
        return PATH_TO_RACER_BINARY
    return utils.PathToFirstExistingExecutable( [ 'racer' ] )

def _FindRustSource( user_options ):
    if user_options.get( 'rust_source_path' ):
        return user_options[ 'rust_source_path' ]

    return os.environ.get( 'RUST_SRC_PATH' )

def _StartRacer( binary, environ ):
    return utils.SafePopen(
      [ binary, 'daemon' ],
      stdout    = subprocess.PIPE,
      stdin     = subprocess.PIPE,
      stderr    = subprocess.PIPE,
      env       = environ,
    )
